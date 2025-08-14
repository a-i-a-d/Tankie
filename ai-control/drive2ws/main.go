package main

import (
    "bytes"
    "fmt"
    "net"
    "encoding/json"
    "io"
    "github.com/gorilla/websocket"
    "log"
)

type DriveParams struct {
    Action      string `json:"action"`
    Duration    string `json:"duration"`
    Speed       string `json:"speed"`
}

func main() {
    // Listen for incoming connections on port 5555 
    ln, err := net.Listen("tcp", ":5555")
    if err != nil {
        fmt.Println(err)
        return
    }

    // Connect to Devastator websocket
    ws := connectWebsocket("ws://10.42.0.20/ws")

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
    fmt.Printf("Received: %s\n", buf)

    // Unmarshal json
    var drive = DriveParams{}
    err := json.Unmarshal(buf.Bytes(), &drive)
    if err != nil {
        fmt.Println(err)
        return
    }
    
    // Handle drive parameters 
    go handleDriveParams(drive, ws)
}

func connectWebsocket(url string) *websocket.Conn {
    ws, _, err := websocket.DefaultDialer.Dial(url, nil)
    if err != nil {
        log.Fatal(err)
    }
    fmt.Printf("Websocket connection to %s established", url)

    defer ws.Close()
    return ws 
}

func closeWebsocket(ws *websocket.Conn) {
    err := ws.Close
    if err != nil {
        log.Fatal(err)
    }
}

func handleDriveParams(d DriveParams, ws *websocket.Conn) {
    // Print the drive values 
    fmt.Printf("Received: \n Action: %s, Duration: %s, Speed: %s\n", d.Action, d.Duration, d.Speed)

    //c := connectWebsocket("ws://10.42.0.20/ws")

    // Connect to websocket and send drive data
    //url := "ws://10.42.0.20/ws"
    //c, _, err := websocket.DefaultDialer.Dial(url, nil)

    //if err != nil {
    //    log.Fatal(err)
    //}

    //defer c.Close()

    ws_message := fmt.Sprintf("")
    switch d.Action {
	case "forward":
	    fmt.Printf("Driving forward")
    	    ws_message = fmt.Sprintf("speed=%s,steer=0", d.Speed)

	case "reverse":
	    fmt.Printf("Driving reverse")
    	    ws_message = fmt.Sprintf("speed=-%s,steer=0", d.Speed)

	case "turn_left":
	    fmt.Printf("Turing left")
    	    ws_message = fmt.Sprintf("speed=%s,steer=-45", d.Speed)

	case "turn_right":
	    fmt.Printf("Turing right")
    	    ws_message = fmt.Sprintf("speed=%s,steer=45", d.Speed)

	case "rotate_left":
	    fmt.Printf("Rotating left")
    	    ws_message = fmt.Sprintf("speed=%s,steer=-90", d.Speed)

	case "rotate_right":
	    fmt.Printf("Rotating right")
    	    ws_message = fmt.Sprintf("speed=%s,steer=90", d.Speed)

	default:
	    fmt.Printf("Unknown action")
    }

    err := ws.WriteMessage(websocket.TextMessage, []byte(ws_message))
    if err != nil {
        log.Fatal(err)
    }
}
