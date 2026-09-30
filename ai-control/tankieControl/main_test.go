package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// testTank starts a websocket server that captures every text frame the
// client sends, and returns a connected client plus the capture buffer.
func testTank(t *testing.T) (*websocket.Conn, *[]string) {
	t.Helper()

	var mu sync.Mutex
	var captured []string

	upgrader := websocket.Upgrader{}
	mux := http.NewServeMux()
	mux.HandleFunc("/ws", func(w http.ResponseWriter, r *http.Request) {
		c, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()
		for {
			_, msg, err := c.ReadMessage()
			if err != nil {
				return
			}
			mu.Lock()
			captured = append(captured, string(msg))
			mu.Unlock()
		}
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)

	url := "ws" + strings.TrimPrefix(srv.URL, "http") + "/ws"
	ws, _, err := websocket.DefaultDialer.Dial(url, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { ws.Close() })

	return ws, &captured
}

// waitFor polls until captured holds at least n messages (server read-loop is
// async) or the deadline passes.
func waitFor(captured *[]string, n int) bool {
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if len(*captured) >= n {
			return true
		}
		time.Sleep(10 * time.Millisecond)
	}
	return len(*captured) >= n
}

func last(captured *[]string) string {
	if len(*captured) == 0 {
		return ""
	}
	return (*captured)[len(*captured)-1]
}

func expect(t *testing.T, captured *[]string, n int, want string) {
	t.Helper()
	if !waitFor(captured, n) {
		t.Fatalf("timed out waiting for %d message(s), got %d", n, len(*captured))
	}
	if got := last(captured); got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}

func TestDriveForward(t *testing.T) {
	ws, captured := testTank(t)
	activeSpeed = 0
	drive(Params{Function: "drive", Action: "forward", Speed: "40"}, ws)
	expect(t, captured, 1, `{"cmd":"drive","speed":40,"steer":0}`)
}

func TestDriveReverse(t *testing.T) {
	ws, captured := testTank(t)
	activeSpeed = 0
	drive(Params{Function: "drive", Action: "reverse", Speed: "20"}, ws)
	expect(t, captured, 1, `{"cmd":"drive","speed":-20,"steer":0}`)
}

func TestDriveStop(t *testing.T) {
	ws, captured := testTank(t)
	activeSpeed = 40
	drive(Params{Function: "drive", Action: "stop", Speed: "0"}, ws)
	expect(t, captured, 1, `{"cmd":"drive","speed":0,"steer":0}`)
}

func TestSteerCombinesActiveSpeed(t *testing.T) {
	ws, captured := testTank(t)
	activeSpeed = 40 // e.g. from a previous drive forward
	steer(Params{Function: "steer", Action: "left", Amount: "30"}, ws)
	expect(t, captured, 1, `{"cmd":"drive","speed":40,"steer":-30}`)
}

func TestSteerRightAndStraight(t *testing.T) {
	ws, captured := testTank(t)
	activeSpeed = 20
	steer(Params{Function: "steer", Action: "right", Amount: "90"}, ws)
	expect(t, captured, 1, `{"cmd":"drive","speed":20,"steer":90}`)
	steer(Params{Function: "steer", Action: "straight", Amount: "0"}, ws)
	expect(t, captured, 2, `{"cmd":"drive","speed":20,"steer":0}`)
}

func TestCameraPanTilt(t *testing.T) {
	ws, captured := testTank(t)
	camera(Params{Function: "camera", Action: "pan_left", Degree: "30"}, ws)
	expect(t, captured, 1, `{"cmd":"pan","angle":60}`)
	camera(Params{Function: "camera", Action: "pan_right", Degree: "30"}, ws)
	expect(t, captured, 2, `{"cmd":"pan","angle":120}`)
	camera(Params{Function: "camera", Action: "tilt_up", Degree: "30"}, ws)
	expect(t, captured, 3, `{"cmd":"tilt","angle":60}`)
	camera(Params{Function: "camera", Action: "tilt_down", Degree: "30"}, ws)
	expect(t, captured, 4, `{"cmd":"tilt","angle":120}`)
}

func TestCameraCenter(t *testing.T) {
	ws, captured := testTank(t)
	camera(Params{Function: "camera", Action: "center", Degree: "0"}, ws)
	if !waitFor(captured, 2) {
		t.Fatalf("timed out waiting for 2 messages, got %d", len(*captured))
	}
	if got := (*captured)[len(*captured)-2]; got != `{"cmd":"pan","angle":90}` {
		t.Fatalf("pan got %q", got)
	}
	if got := (*captured)[len(*captured)-1]; got != `{"cmd":"tilt","angle":90}` {
		t.Fatalf("tilt got %q", got)
	}
}
