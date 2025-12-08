# AI Tank Control

This folder contains code to connect an AI to the tank and drive it. This is experimental. The idea is to send images from the camera to an multimodal LLM that then uses tools to control the tank.

Originally planned to work with LocalAGI, it currently only works with LocalAI, since LocalAGI has a bug that prevents us from sending json messages to the Response API (https://github.com/mudler/LocalAGI/issues/340). The LocalAI integration is a rather basic python script that was mainly created to get some AI control working until the LocalAGI issue is fixed. 

# What is what?

## LocalAGI

This part is still incomplete and lacks sending video images to the model.

For LocalAGI Functions can be used by agents to send control commands to the tank. Since functions are limited to LocalAGI integrated go modules, all commands go through the `tankieControl` wrapper, that handles the websocket connection to the tank. 


## LocalAI

For using the tank with LocalAI, use the ai_control.py script. It's more a proof of concept at the moment and requires you to edit the global variables for your setup.
