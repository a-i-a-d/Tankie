package main

import (
    "bytes"
    "fmt"
    "net"
    "encoding/json"
    "io"
    "github.com/gorilla/websocket"
    "log"
    "flag"
)

type Params struct {
    Function    string 	`json:"function"`
    Action      string 	`json:"action"`
    Duration    int 	`json:"duration"`
    Speed       int 	`json:"speed"`
    Amount	int 	`json:"amount"`
}

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
    var drive = Params{}
    err := json.Unmarshal(buf.Bytes(), &drive)
    if err != nil {
        fmt.Println(err)
        return
    }
    
    // Handle drive parameters 
    go handleParams(drive, ws)
}

func connectWebsocket(url string) *websocket.Conn {
    ws, _, err := websocket.DefaultDialer.Dial(url, nil)
    if err != nil {
        log.Fatal(err)
    }
    fmt.Printf("Websocket connection to %s established", url)

    //defer ws.Close()
    return ws 
}

func closeWebsocket(ws *websocket.Conn) {
    err := ws.Close
    if err != nil {
        log.Fatal(err)
    }
}

func handleParams(d Params, ws *websocket.Conn) {
    fmt.Printf("Function: %s", d.Function)

    switch d.Function {
        case "drive":
            go drive(d, ws)

        case "steer":
            go steer(d, ws)

	case "camera":
            go camera(d, ws)

	default:
            fmt.Printf("Unknown action")
            return
    }
}

func drive(d Params,ws *websocket.Conn) {
    fmt.Printf("Action: %s, Duration: %s, Speed: %s\n", d.Action, d.Duration, d.Speed)
    Speed := 0
    switch d.Action {
        case "forward":
            fmt.Printf("Driving forward")
	    Speed = d.Speed

        case "reverse":
            fmt.Printf("Driving reverse")
	    Speed = -d.Speed

	case "stop":
	    fmt.Printf("Stopping")
    	    Speed = 0

        default:
            fmt.Printf("Unknown action")
            return
    }

    ws_message := fmt.Sprintf("speed=%s", Speed)
    err := ws.WriteMessage(websocket.TextMessage, []byte(ws_message))
    if err != nil {
        log.Fatal(err)
    }

    return
}
	
func steer(d Params,ws *websocket.Conn) {
    fmt.Printf("Action: %s, Amount: %s\n", d.Action, d.Amount)
    Amount := 0
    switch d.Action {
	case "left":
	    fmt.Printf("Turing left")
	    Amount = -d.Amount

	case "right":
	    fmt.Printf("Turing right")
	    Amount = d.Amount

	case "straight":
	    fmt.Printf("Driving straight")
	    Amount = 0 

        default:
            fmt.Printf("Unknown action")
            return
    }

    ws_message := fmt.Sprintf("steer=%s", Amount)
    err := ws.WriteMessage(websocket.TextMessage, []byte(ws_message))
    if err != nil {
        log.Fatal(err)
    }

    return
}

func camera(d Params,ws *websocket.Conn) {
    fmt.Printf("Action: %s, Amount: %s\n", d.Action, d.Amount)
    ws_message := ""
    switch d.Action {
       	case "up":
	    fmt.Printf("Camera up")
    	    ws_message = fmt.Sprintf("tilt=%s", d.Amount)

	case "down":
	    fmt.Printf("Camera down")
    	    ws_message = fmt.Sprintf("tilt=%s", -d.Amount)

	case "left":
	    fmt.Printf("Camera left")
    	    ws_message = fmt.Sprintf("pan=%s", -d.Amount)

	case "right":
	    fmt.Printf("Camera right")
    	    ws_message = fmt.Sprintf("pan=%s", d.Amount)

	case "center":
	    fmt.Printf("Camera centerd")
    	    ws_message = fmt.Sprintf("tilt=%s,pan=%s",0,0)

        default:
            fmt.Printf("Unknown action")
            return
    }

    err := ws.WriteMessage(websocket.TextMessage, []byte(ws_message))
    if err != nil {
        log.Fatal(err)
    }
    
    return
}
