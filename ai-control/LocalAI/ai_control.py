import json
import cv2
import os
import socket
import threading
import base64
from datetime import datetime


from openai import OpenAI
from termcolor import colored

GPT_MODEL = "moondream2-20250414"
API_BASE_URL='http://192.168.1.5:8081/v1'
API_KEY='sk-0123456789'
#VIDEO_URL='http://10.42.0.1:8888/cam/index.m3u8'
VIDEO_URL='http://192.168.100.10:8888/cam/index.m3u8'

# Serial bridge (issue #33, architecture #20): the AI no longer talks to the
# ESP8266's WebSocket. It sends JSON commands to the Pi bridge daemon over
# the Unix socket (one JSON line in -> one JSON line out) and reads the tank
# state from the bridge state store. The bridge owns the 250 ms drive
# keep-alive that re-arms the ESP watchdog, so no re-issue thread is needed
# here anymore.
BRIDGE_SOCKET = os.environ.get("TANKIE_BRIDGE_SOCK", "/run/tankie/bridge.sock")
STATE_FILE = os.environ.get("TANKIE_STATE_FILE", "/var/lib/tankie/state.json")
BRIDGE_TIMEOUT_S = 3.0

# Font settings for text overlay on images
font                   = cv2.FONT_HERSHEY_SIMPLEX
bottomLeftCornerOfText = (0,20)
fontScale              = 0.3
fontColor              = (255,255,255)
thickness              = 1
lineType               = 2

system_message = {
    "role": "system",
    "content": "You are controlling a toy tank. You observe its surroundings through images from an on board camera. You can control the direction the camera is looking and drive the tank with tools you have. Follow instructions on where to drive the tank. Without instructions, explore the environment."
}

user_prompt = 'Explain the current situation in 10 words or less. Predict the next situation in 10 words or less.'


camera_position = {
    "pan":  90,
    "tilt": 90
}

# Autonomous drive profile: safety limits for AI-issued drive commands.
# Deliberately slower than the manual range (±255) so a misbehaving model
# cannot drive the tank at full speed. This mirrors the bridge-side
# auto_profile (raspberry_pi/conf/serial_bridge.yaml), which is the real
# enforcement layer (issue #31); the client-side pre-clamp is a UX mirror
# so the model sees the limit immediately.
AUTO_PROFILE = {
    "max_speed": 40,          # clamp AI speed to this value
    "max_steer": 120,         # clamp AI steer to this value
}

tank = {
    "speed": 0,
    "steer": 0
}

tools = [
    {
        "type": "function",
        "function": {
            "name": "camera_control",
            "description": "Use this function to control the camera",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "left", "right", "center"],
                        "description": "The direction the camera is moved to or centering the camera"
                    },
                    "amount": {
                        "type": "integer",
                        "enum": [0, 30, 60, 90],
                        "description": "The amount the camera should move in the chosen direction"
                    }
                },
                "required": ["direction"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "camera_sweep",
            "description": "Use this function to sweep the camera across a range of angles (e.g. scan the room)",
            "parameters": {
                "type": "object",
                "properties": {
                    "axis": {
                        "type": "string",
                        "enum": ["pan", "tilt"],
                        "description": "Which camera axis to sweep"
                    },
                    "from": {
                        "type": "integer",
                        "description": "Start angle (0-180)"
                    },
                    "to": {
                        "type": "integer",
                        "description": "End angle (0-180)"
                    },
                    "steps": {
                        "type": "integer",
                        "description": "Number of steps (1-50)"
                    }
                },
                "required": ["axis", "from", "to", "steps"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "drive_tank",
            "description": "Use this function to drive the tank",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["forward", "reverse", "left", "right", "stop"],
                        "description": "The direction to drive the tank to or stop "
                    },
                    "speed": {
                        "type": "integer",
                        "enum": [0, 20, 40, 80, 100],
                        "description": "The speed of the tank"
                    }
                },
                "required": ["direction", "speed"],
            },
        }
    }
]


# bufferless VideoCapture, captures only the last available image from video stream.
class VideoCapture:
    def __init__(self, name):
        self.cap = cv2.VideoCapture(name)
        self.lock = threading.Lock()
        self.t = threading.Thread(target=self._reader)
        self.t.daemon = True
        self.t.start()

    # grab frames as soon as they are available
    def _reader(self):
        while True:
            with self.lock:
                ret = self.cap.grab()
            if not ret:
                break

    # retrieve latest frame
    def read(self):
        with self.lock:
            _, frame = self.cap.retrieve()
        return frame


# base64 encode function
def encode_image_to_base64(frame):
    _, buffer = cv2.imencode(".jpg", frame)
    return base64.b64encode(buffer).decode('utf-8')


def bridge_command(cmd):
    """Send one JSON command to the bridge daemon (issue #33).

    One JSON line in -> one JSON line out over the Unix socket. Any failure
    (socket missing, daemon down, timeout, bad reply) degrades gracefully to
    {"ok": False, "error": "..."} so the AI loop never crashes on the link.
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(BRIDGE_TIMEOUT_S)
    try:
        s.connect(BRIDGE_SOCKET)
        s.sendall((json.dumps(cmd) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    except (OSError, socket.timeout) as e:
        return {"ok": False, "error": f"bridge unreachable: {e}"}
    finally:
        s.close()
    if not data:
        return {"ok": False, "error": "bridge returned no response"}
    try:
        reply = json.loads(data.decode().strip())
    except ValueError:
        return {"ok": False, "error": "bridge returned invalid JSON"}
    if not isinstance(reply, dict):
        return {"ok": False, "error": "bridge returned a non-object reply"}
    return reply


def read_state():
    """Read the bridge state store (issue #33).

    The bridge daemon writes /var/lib/tankie/state.json on every update
    (last_state, link_up, watchdog_fired, ...). Missing/corrupt file -> {}.
    """
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def surface_feedback(state=None):
    """Human-readable watchdog/link feedback for the AI loop (issue #33).

    Returns the feedback string (or empty string when all is well) and
    clears the watchdog flag in the state store so it is not re-surfaced
    on the next iteration.
    """
    if state is None:
        state = read_state()
    parts = []
    if state.get("watchdog_fired"):
        parts.append("watchdog fired on the tank: motors stopped, camera recentered")
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({**state, "watchdog_fired": False}, f, indent=2)
        except OSError:
            pass  # read-only store: keep surfacing until the flag clears
    if not state.get("link_up"):
        parts.append("serial link to the tank is down — commands are not reaching the tank")
    return "; ".join(parts)


def sync_from_state(state=None):
    """Pull the applied pan/tilt/speed/steer from the bridge state store.

    Keeps the local dicts in step with what the tank actually reports, so a
    watchdog recenter or a manual command via the CLI is reflected here.
    """
    if state is None:
        state = read_state()
    last = state.get("last_state")
    if isinstance(last, dict):
        for key in ("pan", "tilt"):
            if isinstance(last.get(key), (int, float)):
                camera_position[key] = last[key]
        for key in ("speed", "steer"):
            if isinstance(last.get(key), (int, float)):
                tank[key] = last[key]
    return state


def pretty_print_conversation(messages):
    role_to_color = {
        "system": "red",
        "user": "green",
        "assistant": "blue",
        "function": "magenta",
    }

    for message in messages:
        if message["role"] == "system":
            print(colored(f"system: {message['content']}\n", role_to_color[message["role"]]))
        elif message["role"] == "user":
            print(colored(f"user: {message['content']}\n", role_to_color[message["role"]]))
        elif message["role"] == "assistant" and message.get("tool_calls"):
            print(colored(f"assistant: {message['tool_calls']}\n", role_to_color[message["role"]]))
        elif message["role"] == "assistant" and not message.get("tool_calls"):
            print(colored(f"assistant: {message['content']}\n", role_to_color[message["role"]]))
        elif message["role"] == "function":
            print(colored(f"function ({message['name']}): {message['content']}\n", role_to_color[message["role"]]))


def drive_tank(speed, direction):
    if speed < 0 or speed > 100:
        print ("Invalid amount")
        return("Invalid amount, Valid values are 0 - 100")

    # Autonomous drive profile: clamp to the safe AI speed limit (mirror of
    # the bridge-side auto_profile, which is the enforcement layer — #31).
    speed = min(speed, AUTO_PROFILE["max_speed"])

    if direction == "forward":
        tank["speed"] = speed

    if direction == "reverse":
        tank["speed"] = -speed

    if direction == "stop":
        tank["speed"] = 0

    if direction == "left":
        tank["steer"] = -90
        tank["speed"] = speed

    if direction == "right":
        tank["steer"] = 90
        tank["speed"] = speed

    # Autonomous drive profile: clamp steer to the safe AI steer limit too.
    tank["steer"] = max(-AUTO_PROFILE["max_steer"],
                        min(AUTO_PROFILE["max_steer"], tank["steer"]))

    # One combined drive object over the bridge (issue #33). The bridge
    # re-sends the active drive command every 250 ms (keep-alive), so the
    # ESP watchdog stays re-armed while the AI thinks between frames.
    reply = bridge_command({"cmd": "drive", "speed": tank["speed"], "steer": tank["steer"]})
    print(f"Camera position: {camera_position} Tank state: {tank}")
    print("Sending bridge command: ", json.dumps({"cmd": "drive", "speed": tank["speed"], "steer": tank["steer"]}))
    print("Bridge reply: ", reply)
    feedback = surface_feedback()
    if reply.get("ok"):
        return f"ok: drive speed={tank['speed']} steer={tank['steer']}" + (f"; {feedback}" if feedback else "")
    return f"error: {reply.get('error', 'unknown bridge error')}" + (f"; {feedback}" if feedback else "")


def camera_control(amount, direction):
    if amount < 0 or amount > 90:
        print ("Invalid amount")
        return("Invalid amount, Valid values are 0 - 90")

    print("Moving camera position ", amount, " degree ", direction)
    if direction == "center":
        # Use the real center command (issue #31): pan=90 + tilt=90 in one ack.
        camera_position["pan"] = 90
        camera_position["tilt"] = 90
        reply = bridge_command({"cmd": "center"})
        print("Sending bridge command: ", json.dumps({"cmd": "center"}))
        print("Bridge reply: ", reply)
        feedback = surface_feedback()
        if reply.get("ok"):
            return (f"ok: camera centered (pan=90 tilt=90)"
                    + (f"; {feedback}" if feedback else ""))
        return (f"error: {reply.get('error', 'unknown bridge error')}"
                + (f"; {feedback}" if feedback else ""))

    if direction == "up":
        camera_position["tilt"] = 90 + amount

    elif direction == "down":
        camera_position["tilt"] = 90 - amount

    elif direction == "left":
        camera_position["pan"] = 90 - amount

    elif direction == "right":
        camera_position["pan"] = 90 + amount

    else:
        print("Unknown direction")
        return("Invalid direction")

    # Contract pan/tilt objects over the bridge (issue #33).
    pan_reply = bridge_command({"cmd": "pan", "angle": camera_position["pan"]})
    tilt_reply = bridge_command({"cmd": "tilt", "angle": camera_position["tilt"]})
    print("Sending bridge command: ", json.dumps({"cmd": "pan", "angle": camera_position["pan"]}))
    print("Sending bridge command: ", json.dumps({"cmd": "tilt", "angle": camera_position["tilt"]}))
    print("Bridge reply: ", pan_reply, tilt_reply)
    feedback = surface_feedback()
    if pan_reply.get("ok") and tilt_reply.get("ok"):
        return (f"ok: camera pan={camera_position['pan']} tilt={camera_position['tilt']}"
                + (f"; {feedback}" if feedback else ""))
    return (f"error: pan={pan_reply.get('error', 'unknown')} tilt={tilt_reply.get('error', 'unknown')}"
            + (f"; {feedback}" if feedback else ""))


def camera_sweep(axis="pan", frm=0, to=180, steps=20):
    """Sweep the camera across a range (issue #31).

    The ESP steps the servo non-blockingly and emits
    {"type":"sweep","axis":...,"done":true} on completion; `stop` cancels.
    """
    if axis not in ("pan", "tilt"):
        print("Invalid axis")
        return("Invalid axis, valid values are pan or tilt")
    cmd = {"cmd": "sweep", "axis": axis, "from": frm, "to": to, "steps": steps}
    print("Sweeping camera ", axis, frm, "->", to, "over", steps, "steps")
    reply = bridge_command(cmd)
    print("Sending bridge command: ", json.dumps(cmd))
    print("Bridge reply: ", reply)
    feedback = surface_feedback()
    if reply.get("ok"):
        return (f"ok: sweep {axis} {frm}->{to} over {steps} steps"
                + (f"; {feedback}" if feedback else ""))
    return (f"error: {reply.get('error', 'unknown bridge error')}"
            + (f"; {feedback}" if feedback else ""))


def call_function(name,args):
    print("Calling ", name, " with ", args)
    if name == "camera_control":
        return camera_control(**args)
    if name == "camera_sweep":
        # "from"/"to" are reserved words, so map them to frm/to explicitly.
        return camera_sweep(axis=args.get("axis", "pan"),
                            frm=args.get("from", 0),
                            to=args.get("to", 180),
                            steps=args.get("steps", 20))
    if name == "drive_tank":
        return drive_tank(**args)
    else:
        return "Error, tool does not exist"


def tool_chat(frame, client):


    messages = []
    messages.append(system_message)

    user_message = {
        "role": "user",
        "content": [
            user_prompt,
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame}"}}
        ]
    }
    messages.append(user_message)

    params = {
        "model": GPT_MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": 500,
    }
    response = client.chat.completions.create(**params)

    response_message = response.choices[0].message
    print(f"\n\nresponse_message: {response.choices[0].message.tool_calls}\n\n")

    messages.append(response_message.to_dict())
    pretty_print_conversation(messages)

    tool_calls = response_message.tool_calls
    while tool_calls:
        # Model returned tool call, execute it and prompt model with result
        tool_call_id = tool_calls[0].id
        tool_name = tool_calls[0].function.name
        tool_args = json.loads(tool_calls[0].function.arguments)

        results = call_function(tool_name, tool_args)

        messages.append({
            "role":"tool",
            "tool_call_id":tool_call_id,
            "name": tool_name,
            "content":results
        })

        # Call model with tool response
        response_with_function_call = client.chat.completions.create(
            model=GPT_MODEL,
            messages=messages,
        )

        # Get possible tool calls from response
        tool_calls = response_with_function_call.choices[0].message.tool_calls
        print(f"tool_calls: {tool_calls}")
        #print(f"Result: {response_with_function_call.choices[0].message.content}")
        return (f"Result: {response_with_function_call.choices[0].message.content}")

    else:
        # Model did not identify a function to call, result can be returned to the user
        #print(response_message.content)
        return response_message.content



def main():

    client = OpenAI(api_key=API_KEY, base_url=API_BASE_URL)

    video = VideoCapture(VIDEO_URL)
    while(True):
        frame = video.read()
        if frame is not None:

            # Stay in step with the tank and surface watchdog/link feedback
            # (issue #33): the bridge is the source of truth now.
            state = sync_from_state()
            feedback = surface_feedback(state)
            if feedback:
                print("[tank]", feedback)

            base64_image = encode_image_to_base64(frame)
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            generated_text = tool_chat(base64_image, client)
            print(f"Timestamp: {timestamp}, Generated Text: {generated_text}")

            cv2.imshow('frame',frame)
            if cv2.waitKey(22) & 0xFF == ord('q'):
                break

            #time.sleep(1)

        else:
            print("Frame is null")

if __name__ == "__main__":
    main()
