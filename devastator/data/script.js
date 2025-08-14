var gateway = `ws://${window.location.hostname}/ws`;
var websocket;
var streamUrl = "http://10.42.0.1:8889/cam/";
var stream = false;
window.addEventListener('load', onLoad);

function connectStream() {
    if (!stream) {
        document.getElementById('videoStream').src = streamUrl;
	document.getElementById('streamBtn').value = "Stop video stream";
    	console.log('Enabling stream');
	stream = true;
    } else {
	document.getElementById('videoStream').src = "/tankie.png";
	document.getElementById('streamBtn').value = "Start video stream";
    	console.log('Disabling stream');
	stream = false;
    }
}

function initWebSocket() {
    console.log('Trying to open a WebSocket connection...');
    websocket = new WebSocket(gateway);
    websocket.onopen    = onOpen;
    websocket.onclose   = onClose;
    websocket.onmessage = onMessage;
}

function onOpen(event) {
    console.log('Connection opened');
}

function onClose(event) {
    console.log('Connection closed');
    setTimeout(initWebSocket, 2000);
}

function onMessage(event) {
    console.log('Got message');
    console.log(event.data);
    var messageObj = JSON.parse(event.data);
    console.log(messageObj["battery"]);
    battery.value = messageObj["battery"];
}

function onLoad(event) {
    initWebSocket();
}

var lastSpeed = 0;
var lastSteer = 0;
var lastPan = 0;
var lastTilt = 0;

function handleJoy2Data() {
    var currentPan = parseInt((Joy2.GetX()*90/100)+90);
    var currentTilt = parseInt(-((Joy2.GetY()*90/100)-90));

    if (currentPan != lastPan) {
        console.log("Pan: ", currentPan);
        joy2Pan.value = currentPan;
        websocket.send('pan='+currentPan);
        lastPan = currentPan;
    }

    if (currentTilt != lastTilt) {
        console.log("Tilt: ", currentTilt);
        joy2Tilt.value = currentTilt;
        websocket.send('tilt='+currentTilt);
        lastTilt = currentTilt;
    }
}

function handleJoy1Data() {
    var currentSteer = parseInt(Joy1.GetX()*254/100);
    var currentSpeed = parseInt(Joy1.GetY()*254/100);

    if (currentSteer != lastSteer) {
        console.log("Steer: ", currentSteer);
        joy1Steer.value = currentSteer;
        websocket.send('steer='+currentSteer);
        lastSteer = currentSteer;
    }

    if (currentSpeed != lastSpeed) {
        console.log("Speed: ", currentSpeed);
        joy1Speed.value = currentSpeed;
        websocket.send('speed='+currentSpeed);
        lastSpeed = currentSpeed;
    }
}

// Create JoyStick object into the DIV 'joy1Div'
var joy1Param = { "title": "joystick1", "autoReturnToCenter": true };
var Joy1 = new JoyStick('joy1Div', joy1Param);

var joy1Steer = document.getElementById("joy1Steer");
var joy1Speed = document.getElementById("joy1Speed");

// Create JoyStick object into the DIV 'joy2Div'
var joy2Param = { "title": "joystick2", "autoReturnToCenter": false };
var Joy2 = new JoyStick('joy2Div', joy2Param);

var joy2Pan = document.getElementById("joy2Pan");
var joy2Tilt = document.getElementById("joy2Tilt");

setInterval(handleJoy1Data, 50);
setInterval(handleJoy2Data, 50);
