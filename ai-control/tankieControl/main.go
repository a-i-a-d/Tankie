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
    "strconv"
)

type Params struct {
    Function    string 	`json:"function"`
    Action      string 	`json:"action"`
    Speed       string 	`json:"speed"`
    Amount	string 	`json:"amount"`
    Degree	string	`json:"degree"`
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
    fmt.Printf("Websocket connection to %s established\n", url)

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

func drive(d Params,ws *websocket.Conn) {
    fmt.Printf("Action: %s, Speed: %s\n", d.Action, d.Speed)

    ws_message := "" 

    switch d.Action {
        case "forward":
            fmt.Printf("Driving forward\n")
            ws_message = fmt.Sprintf("speed=%s", d.Speed)

        case "reverse":
            fmt.Printf("Driving reverse\n")
            ws_message = fmt.Sprintf("speed=-%s", d.Speed)

	case "stop":
	    fmt.Printf("Stopping\n")
            ws_message = fmt.Sprintf("speed=0")

        default:
            fmt.Printf("Unknown action\n")
            return
    }

    fmt.Printf("Message: %s\n", ws_message)
    err := ws.WriteMessage(websocket.TextMessage, []byte(ws_message))
    if err != nil {
        log.Fatal(err)
    }

    return
}
	
func steer(d Params,ws *websocket.Conn) {
    fmt.Printf("Action: %s, Amount: %s\n", d.Action, d.Amount)

    ws_message := ""

    switch d.Action {
	case "left":
	    fmt.Printf("Turing left\n")
            ws_message = fmt.Sprintf("steer=-%s\n", d.Amount)

	case "right":
	    fmt.Printf("Turing right\n")
            ws_message = fmt.Sprintf("steer=%s\n", d.Amount)

	case "straight":
	    fmt.Printf("Driving straight\n")
            ws_message = fmt.Sprintf("steer=0\n")

        default:
            fmt.Printf("Unknown action\n")
            return
    }

    fmt.Printf("Message: %s\n", ws_message)
    err := ws.WriteMessage(websocket.TextMessage, []byte(ws_message))
    if err != nil {
        log.Fatal(err)
    }

    return
}

func camera(d Params,ws *websocket.Conn) {
    fmt.Printf("Action: %s, Degree: %s\n", d.Action, d.Degree)

    ws_message := ""
    amount, err := strconv.Atoi(d.Degree)
    if err != nil {
        log.Fatal(err)
    }

    switch d.Action {
        case "pan_left":
	    fmt.Printf("pan left\n")
    	    ws_message = fmt.Sprintf("pan=%s", strconv.Itoa(90 - amount))

        case "pan_right":
	    fmt.Printf("pan right\n")
    	    ws_message = fmt.Sprintf("pan=%s", strconv.Itoa(90 + amount))

	case "tilt_up":
	    fmt.Printf("tilt up\n")
    	    ws_message = fmt.Sprintf("tilt=%s", strconv.Itoa(90 - amount))

	case "tilt_down":
	    fmt.Printf("tilt down\n")
    	    ws_message = fmt.Sprintf("tilt=%s", strconv.Itoa(90 + amount))

	case "center":
	    fmt.Printf("center camera\n")
    	    ws_message = fmt.Sprintf("tilt=90,pan=90")

	default:
            fmt.Printf("Unknown action\n")
            return
    }

    /*switch d.Action {
       	case "up":
	    fmt.Printf("Camera up\n")
    	    //ws_message = fmt.Sprintf("tilt=%s", d.Amount)

	case "down":
	    fmt.Printf("Camera down\n")
    	    //ws_message = fmt.Sprintf("tilt=-%s", d.Amount)

	case "left":
	    fmt.Printf("Camera left\n")
    	    //ws_message = fmt.Sprintf("pan=-%s", d.Amount)

	case "right":
	    fmt.Printf("Camera right\n")
    	    //ws_message = fmt.Sprintf("pan=%s", d.Amount)

	case "center":
	    fmt.Printf("Camera centerd\n")
    	    ws_message = fmt.Sprintf("tilt=%s,pan=%s",90,90)

        default:
            fmt.Printf("Unknown action\n")
            return
    }*/

    fmt.Printf("Message: %s\n", ws_message)
    err = ws.WriteMessage(websocket.TextMessage, []byte(ws_message))
    if err != nil {
        log.Fatal(err)
    }
    
    return
}
