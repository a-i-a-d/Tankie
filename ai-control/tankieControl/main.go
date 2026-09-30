package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"strconv"
	"sync"

	"github.com/gorilla/websocket"
)

type Params struct {
	Function string `json:"function"`
	Action   string `json:"action"`
	Speed    string `json:"speed"`
	Amount   string `json:"amount"`
	Degree   string `json:"degree"`
}

// TankCmd is the JSON protocol contract (issue #32) spoken by the ESP /ws
// endpoint — the same NDJSON shapes as the serial link (tankie/serialproto.h).
// Drive carries speed AND steer in one object; pan/tilt carry an angle.
type TankCmd struct {
	Cmd   string `json:"cmd"`
	Speed *int   `json:"speed,omitempty"`
	Steer *int   `json:"steer,omitempty"`
	Angle *int   `json:"angle,omitempty"`
}

// activeSpeed tracks the tank's last commanded speed so a steer command can be
// combined with it into one contract drive object (issue #39: the drive
// command carries both speed and steer, whereas the LocalAGI wrapper sends
// them as separate actions).
var (
	stateMu     sync.Mutex
	activeSpeed = 0
)

func main() {
	// Handle parameters
	wsPtr := flag.String("websocket", "10.42.0.20:80", "IP:PORT for websocket server")
	flag.Parse()

	fmt.Println("Websocket URL:", *wsPtr)

	// Listen for incoming connections on port 5555
	ln, err := net.Listen("tcp", ":5555")
	if err != nil {
		fmt.Println(err)
		return
	}

	// Connect to Devastator websocket
	ws := connectWebsocket("ws://" + *wsPtr + "/ws")

	go func() {
		for {
			_, message, err := ws.ReadMessage()
			if err != nil {
				log.Println("ReadMessage() error:", err)
				return
			}
			log.Printf("Received:\n%s\n\n", message)
		}
	}()

	// Accept incoming connections and handle them
	for {
		tcp, err := ln.Accept()
		if err != nil {
			fmt.Println(err)
			continue
		}

		// Handle the connection in a new goroutine
		go handleConnection(tcp, ws)
	}
}

func handleConnection(tcp net.Conn, ws *websocket.Conn) {
	// Close the connection when we're done
	defer tcp.Close()

	// Receive data
	var buf bytes.Buffer
	io.Copy(&buf, tcp)
	fmt.Printf("Received: %s\n", buf.String())

	// Unmarshal json
	var drive = Params{}
	err := json.Unmarshal(buf.Bytes(), &drive)
	if err != nil {
		fmt.Println(err)
		return
	}

	// Handle drive parameters
	go handleParams(drive, ws)
}

func handleParams(d Params, ws *websocket.Conn) {
	fmt.Printf("Function: %s\n", d.Function)

	switch d.Function {
	case "drive":
		go drive(d, ws)

	case "steer":
		go steer(d, ws)

	case "camera":
		go camera(d, ws)

	default:
		fmt.Printf("Unknown action\n")
		return
	}
}

func connectWebsocket(url string) *websocket.Conn {
	ws, _, err := websocket.DefaultDialer.Dial(url, nil)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("Websocket connection to %s established\n", url)

	//defer ws.Close()
	return ws
}

// sendCmd sends one contract JSON object to the tank (issue #32/#39).
func sendCmd(ws *websocket.Conn, c TankCmd) {
	payload, err := json.Marshal(c)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("Message: %s\n", payload)
	if err := ws.WriteMessage(websocket.TextMessage, payload); err != nil {
		log.Fatal(err)
	}
}

// parseValue converts a LocalAGI-provided numeric string to an int.
func parseValue(s string, what string) int {
	v, err := strconv.Atoi(s)
	if err != nil {
		log.Fatalf("invalid %s value %q: %v", what, s, err)
	}
	return v
}

func drive(d Params, ws *websocket.Conn) {
	fmt.Printf("Action: %s, Speed: %s\n", d.Action, d.Speed)

	switch d.Action {
	case "forward":
		fmt.Printf("Driving forward\n")
		v := parseValue(d.Speed, "speed")
		stateMu.Lock()
		activeSpeed = v
		stateMu.Unlock()
		sendCmd(ws, TankCmd{Cmd: "drive", Speed: intPtr(v), Steer: intPtr(0)})

	case "reverse":
		fmt.Printf("Driving reverse\n")
		v := -parseValue(d.Speed, "speed")
		stateMu.Lock()
		activeSpeed = v
		stateMu.Unlock()
		sendCmd(ws, TankCmd{Cmd: "drive", Speed: intPtr(v), Steer: intPtr(0)})

	case "stop":
		fmt.Printf("Stopping\n")
		stateMu.Lock()
		activeSpeed = 0
		stateMu.Unlock()
		sendCmd(ws, TankCmd{Cmd: "drive", Speed: intPtr(0), Steer: intPtr(0)})

	default:
		fmt.Printf("Unknown action\n")
		return
	}
}

func steer(d Params, ws *websocket.Conn) {
	fmt.Printf("Action: %s, Amount: %s\n", d.Action, d.Amount)

	// The contract drive command carries both speed and steer in one object,
	// so combine the requested steer with the last active speed (issue #39).
	stateMu.Lock()
	speed := activeSpeed
	stateMu.Unlock()

	switch d.Action {
	case "left":
		fmt.Printf("Turing left\n")
		sendCmd(ws, TankCmd{Cmd: "drive", Speed: intPtr(speed), Steer: intPtr(-parseValue(d.Amount, "steer"))})

	case "right":
		fmt.Printf("Turing right\n")
		sendCmd(ws, TankCmd{Cmd: "drive", Speed: intPtr(speed), Steer: intPtr(parseValue(d.Amount, "steer"))})

	case "straight":
		fmt.Printf("Driving straight\n")
		sendCmd(ws, TankCmd{Cmd: "drive", Speed: intPtr(speed), Steer: intPtr(0)})

	default:
		fmt.Printf("Unknown action\n")
		return
	}
}

func camera(d Params, ws *websocket.Conn) {
	fmt.Printf("Action: %s, Degree: %s\n", d.Action, d.Degree)

	amount := parseValue(d.Degree, "degree")

	switch d.Action {
	case "pan_left":
		fmt.Printf("pan left\n")
		sendCmd(ws, TankCmd{Cmd: "pan", Angle: intPtr(90 - amount)})

	case "pan_right":
		fmt.Printf("pan right\n")
		sendCmd(ws, TankCmd{Cmd: "pan", Angle: intPtr(90 + amount)})

	case "tilt_up":
		fmt.Printf("tilt up\n")
		sendCmd(ws, TankCmd{Cmd: "tilt", Angle: intPtr(90 - amount)})

	case "tilt_down":
		fmt.Printf("tilt down\n")
		sendCmd(ws, TankCmd{Cmd: "tilt", Angle: intPtr(90 + amount)})

	case "center":
		fmt.Printf("center camera\n")
		sendCmd(ws, TankCmd{Cmd: "pan", Angle: intPtr(90)})
		sendCmd(ws, TankCmd{Cmd: "tilt", Angle: intPtr(90)})

	default:
		fmt.Printf("Unknown action\n")
		return
	}
}

func intPtr(v int) *int { return &v }
