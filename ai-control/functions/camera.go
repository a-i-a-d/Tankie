import (
    "encoding/json"
    "fmt"
    "net"
    "log"
)

type CamParams struct {
    Function    string `json:"function"`
    Action  	string `json:"action"`
    Amount	int `json:"amount"`
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
    fmt.Printf("Amount: %s\n", p.Amount) 

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
	    "Move Camera: up, down, left, right, center",
        },
        "amount": []string{
            "string",
	    "Amount: Absolute amount to move the camera in percent 0-100",
        },
    }
}

func RequiredFields() []string {
    return []string{"action", "amount"}
}
