import (
    "encoding/json"
    "fmt"
    "net"
    "log"
)

type DriveParams struct {
    Action  	string `json:"action"`
    Duration	string `json:"duration"`
    Speed	string `json:"speed"`
}

func Run(config map[string]interface{}) (string, map[string]interface{}, error) {
    p := DriveParams{}
    b, err := json.Marshal(config)
    if err != nil {
        return "", map[string]interface{}{}, err
    }
    if err := json.Unmarshal(b, &p); err != nil {
        return "", map[string]interface{}{}, err
    }

    fmt.Printf("Action: %s\n", p.Action) 
    fmt.Printf("Speed: %s\n", p.Speed) 
    fmt.Printf("Duration: %s\n", p.Duration) 

    conn, err := net.Dial("tcp", "192.168.1.50:5555")
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
	    "Drive action: forward, reverse, turn_left, turn_right, rotate_left, rotate_right",
        },
        "duration": []string{
            "string",
	    "drive duration in milliseconds",
        },
        "speed": []string{
            "string",
            "speed in percent from 0 to 100",
        },
    }
}

func RequiredFields() []string {
    return []string{"action", "duration"}
}
