import (
    "encoding/json"
    "fmt"
    "net"
    "log"
)

type CamParams struct {
    Function    string `json:"function"`
    Action  	string `json:"action"`
    Degree	string `json:"degree"`
}

func Run(config map[string]interface{}) (string, map[string]interface{}, error) {
    wsProxy := "192.168.1.50:5555"

    p := CamParams{}
    b, err := json.Marshal(config)
    if err != nil {
        return "", map[string]interface{}{}, err
    }
    if err := json.Unmarshal(b, &p); err != nil {
        return "", map[string]interface{}{}, err
    }

    p.Function = "camera"
    fmt.Printf("Function: %s\n", p.Function) 
    fmt.Printf("Action: %s\n", p.Action) 
    fmt.Printf("Degree: %s\n", p.Degree) 

    conn, err := net.Dial("tcp", wsProxy)
    if err != nil {
        return "", map[string]interface{}{}, err
    }
    defer conn.Close()

    jsonData, err := json.Marshal(p)
    if err != nil {
        return "error marshaling p", map[string]interface{}{}, err
    }

    _, err = conn.Write([]byte(string(jsonData)))
    if err != nil {
        return "", map[string]interface{}{}, err
    }

    log.Println("Sent", string(jsonData), "to server.")
    return string("Success"), map[string]interface{}{}, nil
}

func Definition() map[string][]string {
    return map[string][]string{
        "action": []string{
            "string",
	    "Action: pan_left, pan_right, tilt_up, tilt_down, center",
        },
        "degree": []string{
            "string",
	    "Degree: pan/tilt degree from 0 to 90",
        },
    }
}

func RequiredFields() []string {
    return []string{"action", "degree"}
}
