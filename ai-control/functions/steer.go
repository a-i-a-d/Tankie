import (
    "encoding/json"
    "fmt"
    "net"
    "log"
)

type SteerParams struct {
    Function	string  `json:"function"`
    Action  	string 	`json:"action"`
    Amount	int 	`json:"amount"`
}

func Run(config map[string]interface{}) (string, map[string]interface{}, error) {
    wsProxy := "192.168.1.50:5555"

    p := SteerParams{}
    b, err := json.Marshal(config)
    if err != nil {
        return "", map[string]interface{}{}, err
    }
    if err := json.Unmarshal(b, &p); err != nil {
        return "", map[string]interface{}{}, err
    }

    p.Function = "steer"
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
	    "Steering action: left, right, center",
        },
        "amount": []string{
            "string",
	    "Steering amount: the amount to steer in percent from 0 to 100",
        },
    }
}

func RequiredFields() []string {
    return []string{"action", "amount"}
}
