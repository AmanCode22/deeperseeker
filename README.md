# DeeperSeeker
An unofficial deepseek website parser.

It uses litellm to expose anthropic and openai compatible endpoints

# Getting started
Firstly you need python to run deeperseeker and git to clone repo.
It is recommended to use venv to install dependencies to prevent breaking system packages.

Firstly download the zip from releases as  the git clone can download unstable code from repo.
Then unzip it.
Nextly, create and activate venv(optional, but recommended)
```bash
python3 -m venv deeperseeker_env
source deeperseeker_env/bin/activate
```
Then, install dependencines
```bash
pip install -r requirements.txt
```

Then, you need your auth token. run
```bash
python3 add_auth_token.py
```
Follow instructions to add your auth token to deeperseeker.

Next you need to setup litellm run
```bash
python3 generate_db.py
```

Now you need to generate cookies, you may need to generate cookies after some days again the script will tell you about the expiry. Also, sometimes in rate limits changing cookie helps.
Firstly install playwright chromium browser(needed one time only after that if cookie expires or rate limit occurs just run script), run
```bash
playwright install chromium
```
Then run cookie finder
```bash
python3 aws_waf_finder.py
```

Now you can run litellm server using
```bash
python3 run.py
```

Deeperseeker uses wasm file from my repo in which I recreated deepseek POW solver: [deepseek_pow_solver](https://github.com/AmanCode22/deepseek_pow_solver)

# Few Important Notes
 - LiteLLM's UI is opened at http://localhost:4000 or your public ip.
 - Litellm UI's default admin password is admin@1234, can be changed in .env file.
 - Currently, every api key represents a chat id, api key can be generated in litellm admin ui.
 - You must not logout from deepseek on the browser from which auth token is extracted or else it will also trigger logout. Clear cookies instead of deepseek website.


# Disclamer
This project is just for educational purpose it is not affilated with deepseek anyway.
