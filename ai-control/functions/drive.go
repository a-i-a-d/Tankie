import (
    "encoding/json"
    "fmt"
    "net"
    "log"
)

type DriveParams struct {
    Function	string `json:"function"`
    Action  	string `json:"action"`
    Speed	int `json:"speed"`
}

func Run(config map[string]interface{}) (string, map[string]interface{}, error) {
    wsProxy := "192.168.1.50:5555"

    p := DriveParams{}
    b, err := json.Marshal(config)
    if err != nil {
        return "", map[string]interface{}{}, err
    }
    if err := json.Unmarshal(b, &p); err != nil {
        return "", map[string]interface{}{}, err
    }

    p.Function = "drive"
    fmt.Printf("Function: %s\n", p.Function) 
    fmt.Printf("Action: %s\n", p.Action) 
    fmt.Printf("Speed: %s\n", p.Speed) 

    conn, err := net.Dial("tcp", wsProxy)
    if err != nil {
        //log.Fatal("Error connecting:", err)
        return "", map[string]interface{}{}, err
    }
    defer conn.Close()

    jsonData, err := json.Marshal(p)
    if err != nil {
        return "error marshaling p", map[string]interface{}{}, err
    }
    //message := string(jsonData)

    //_, err = conn.Write([]byte(message))
    _, err = conn.Write([]byte(string(jsonData)))
    if err != nil {
        //log.Fatal("Error writing:", err)
        return "", map[string]interface{}{}, err
    }

    log.Println("Sent", string(jsonData), "to server.")
    return string("Driving"), map[string]interface{}{}, nil
}

func Definition() map[string][]string {
    return map[string][]string{
        "action": []string{
            "string",
	    "Driving action: forward, reverse, stop",
        },
        "speed": []string{
            "string",
	    "Driving speed: Speed in percent from 0 to 100",
        },
    }
}

func RequiredFields() []string {
    return []string{"action", "speed"}
}
