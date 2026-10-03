// Web UI — JSON protocol per the serial contract (issue #32).
//
// All outgoing tank messages are JSON objects (the NDJSON protocol contract,
// tankie/serialproto.h / README "Serial control protocol"):
//   {"cmd":"drive","speed":N,"steer":M}
//   {"cmd":"pan","angle":N}
//   {"cmd":"tilt","angle":N}
//   {"cmd":"stop"}
//
// Incoming messages are dispatched on "type":
//   {"type":"state","seq":N,"battery":B,"speed":S,"steer":T,"pan":P,"tilt":U,
//    "net_mode":"sta"|"ap","net_ip":"192.168.x.y"}   (issue #46)
//   {"type":"ack","seq":N}
//   {"type":"error","seq":N,"code":C,"field":F}
//   {"type":"watchdog"}
//   {"type":"hello","proto":N,"fw":S}
//
// The same code path works against the ESP WebSocket fallback and the
// future Pi-served WebSocket (#20 step 3).
var gateway = `ws://${window.location.hostname}/ws`;
var websocket;
var streamUrl = "http://10.42.0.1:8889/cam/";
var stream = false;
window.addEventListener('load', onLoad);

function connectStream() {
    if (!stream) {
        document.getElementById('videoStream').src = streamUrl;
        document.getElementById('streamBtn').value = "Stop video stream";
        console.log('Enabling stream');
        stream = true;
    } else {
        document.getElementById('videoStream').src = "/tankie.png";
        document.getElementById('streamBtn').value = "Start video stream";
        console.log('Disabling stream');
        stream = false;
    }
}

function initWebSocket() {
    console.log('Trying to open a WebSocket connection...');
    websocket = new WebSocket(gateway);
    websocket.onopen    = onOpen;
    websocket.onclose   = onClose;
    websocket.onmessage = onMessage;
}

function onOpen(event) {
    console.log('Connection opened');
}

function onClose(event) {
    console.log('Connection closed');
    setTimeout(initWebSocket, 2000);
}

// Short UI hint in the #status line (auto-clears after 4 s).
var statusTimer = null;
function showStatus(text) {
    var el = document.getElementById('status');
    if (!el) return;
    el.textContent = text;
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = setTimeout(function () { el.textContent = ''; }, 4000);
}

function onMessage(event) {
    console.log('Got message:', event.data);
    var msg;
    try {
        msg = JSON.parse(event.data);
    } catch (e) {
        console.warn('Ignoring non-JSON message:', event.data);
        return;
    }
    if (!msg || typeof msg !== 'object') return;

    switch (msg['type']) {
        case 'state':
            // Render the full state broadcast into the on-screen fields.
            if (typeof msg['battery'] === 'number') battery.value = msg['battery'];
            if (typeof msg['speed'] === 'number') joy1Speed.value = msg['speed'];
            if (typeof msg['steer'] === 'number') joy1Steer.value = msg['steer'];
            if (typeof msg['pan'] === 'number') joy2Pan.value = msg['pan'];
            if (typeof msg['tilt'] === 'number') joy2Tilt.value = msg['tilt'];
            // Issue #46: show the network mode + IP so it is visible without
            // the boot console (mode: sta/ap, ip: the ESP's address).
            if (typeof msg['net_mode'] === 'string' || typeof msg['net_ip'] === 'string') {
                var netEl = document.getElementById('netStatus');
                if (netEl) {
                    netEl.value = (msg['net_mode'] || '?') + ' ' + (msg['net_ip'] || '?');
                }
            }
            break;
        case 'ack':
            console.log('ack seq=' + msg['seq']);
            break;
        case 'error':
            console.warn('error seq=' + msg['seq'] + ' code=' + msg['code'] + ' field=' + msg['field']);
            showStatus('error: ' + msg['code'] + (msg['field'] ? ' (' + msg['field'] + ')' : ''));
            break;
        case 'watchdog':
            console.log('watchdog: motors stopped');
            showStatus('watchdog: motors stopped');
            // Reset local last-values so the next joystick move re-sends.
            lastSpeed = 0;
            lastSteer = 0;
            lastPan = 0;
            lastTilt = 0;
            break;
        case 'hello':
            console.log('hello proto=' + msg['proto'] + ' fw=' + msg['fw']);
            break;
        default:
            // Unknown type — ignore (coexistence rule, issue #29 T7).
            console.log('Ignoring unknown message type: ' + msg['type']);
            break;
    }
}

function onLoad(event) {
    initWebSocket();
}

var lastSpeed = 0;
var lastSteer = 0;
var lastPan = 0;
var lastTilt = 0;

// Send a JSON command object over the WebSocket (guarded on readyState so
// the 2 s reconnect window never throws).
function sendCommand(obj) {
    if (!websocket || websocket.readyState !== WebSocket.OPEN) return;
    websocket.send(JSON.stringify(obj));
}

function handleJoy2Data() {
    var currentPan = parseInt((Joy2.GetX()*90/100)+90);
    var currentTilt = parseInt(-((Joy2.GetY()*90/100)-90));

    if (currentPan != lastPan) {
        console.log("Pan: ", currentPan);
        joy2Pan.value = currentPan;
        sendCommand({ "cmd": "pan", "angle": currentPan });
        lastPan = currentPan;
    }

    if (currentTilt != lastTilt) {
        console.log("Tilt: ", currentTilt);
        joy2Tilt.value = currentTilt;
        sendCommand({ "cmd": "tilt", "angle": currentTilt });
        lastTilt = currentTilt;
    }
}

function handleJoy1Data() {
    var currentSteer = parseInt(Joy1.GetX()*254/100);
    var currentSpeed = parseInt(Joy1.GetY()*254/100);

    if (currentSteer == lastSteer && currentSpeed == lastSpeed) return;

    console.log("Speed: ", currentSpeed, " Steer: ", currentSteer);
    joy1Speed.value = currentSpeed;
    joy1Steer.value = currentSteer;

    if (currentSpeed == 0 && currentSteer == 0 &&
        (lastSpeed != 0 || lastSteer != 0)) {
        // Stick returned to center after being active: explicit stop so the
        // ESP watchdog re-arms exactly as the serial path does.
        sendCommand({ "cmd": "stop" });
    } else {
        // One combined drive object (the contract's drive shape).
        sendCommand({ "cmd": "drive", "speed": currentSpeed, "steer": currentSteer });
    }
    lastSpeed = currentSpeed;
    lastSteer = currentSteer;
}

// Create JoyStick object into the DIV 'joy1Div'
var joy1Param = { "title": "joystick1", "autoReturnToCenter": true };
var Joy1 = new JoyStick('joy1Div', joy1Param);

var joy1Steer = document.getElementById("joy1Steer");
var joy1Speed = document.getElementById("joy1Speed");

// Create JoyStick object into the DIV 'joy2Div'
var joy2Param = { "title": "joystick2", "autoReturnToCenter": false };
var Joy2 = new JoyStick('joy2Div', joy2Param);

var joy2Pan = document.getElementById("joy2Pan");
var joy2Tilt = document.getElementById("joy2Tilt");

setInterval(handleJoy1Data, 50);
setInterval(handleJoy2Data, 50);
