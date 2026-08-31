# Setting up deeperseeker in claude desktop
Claude desktop app is available on Linux, MacOS and Windows.
Deeperseeker can be setup on claude desktop app by using it's 3p(third party) mode.

## Setup
For this firstly install claude desktop app from : https://claude.com/download

After this you must have deeperseeker running, claude desktop app needs either endpoint to be running on localhost or must have https support.

In claude desktop click harmburger/three lines menu and click Help->Troubleshooting->Enable Developer Mode

After this claude may take a restart and then again click on harmburger menu and this time you would have developer named menu.
Now click on developer->Configure Third Party Inference and then a popup window will open in which in connection select gateway and in gateway configure the following:

__*Credential Type*__: Static API Key

__*Gateway base URL*__ : http://localhost:4000/ (must be https if not localhost and don't add any /v1,etc)

__*Gateway API key*__ : Add your configured API key or default is dseeker

__*Gateway auth scheme*__ : x-api-key/bearer (DeeperSeeker supports both)
Then click test model discovery and test connection if it works then just save and apply.

If it does not then check your ip, port or api key if still not works then feel free to open issue.

## Enabling Chat with cowork in 3p mode
Chat is by default disabled in claude desktop app for 3p mode and it can be enabled in the same inference configuration popup which can be now opened from by clicking your name in the bottom left corner of claude app and selecting inference configuration.
In it go to Workspace-> Allowed Surfaces -> Turn on chat option

## Allowing external websites to be browsed by agent
By default in 3p mode some specific websites are only allowed to be browsed by the agent.
To see what all websites are allowed, go to same inference configuration page -> Egress.

To allow specific website :

Go to same inference configuration page -> Workspace -> General Restrictions -> Allowed egress host
and here add specific website you want to allow or if you want to allow all just click on `*` button.
