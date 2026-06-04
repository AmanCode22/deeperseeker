import base64
import json
import os
import time

import requests
import wasmtime

wasm_path = "wasm/deepseek_pow_solver.wasm"


def get_headers(auth_token, pow=None):
    headers = {
        "accept": "*/*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "authorization": f"Bearer {auth_token}",
        "content-type": "application/json",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "x-app-version": "2.0.0",
        "x-client-locale": "en_US",
        "x-client-platform": "web",
        "x-client-version": "2.0.0",
    }

    if pow:
        headers["x-ds-pow-response"] = pow
    return headers


def get_cookies():
    if not os.path.exists("aws_cookies_deepseek.json"):
        print(
            "Please generate cookies first! As file aws_cookies_deepseek.json! does not exists."
        )
    cookies = json.load(open("aws_cookies_deepseek.json"))
    if cookies["expiry"] <= time.time():
        print("Cookie expired! Please regenerate it!")
        exit()
    return cookies["cookie"]


COOKIE = get_cookies()


def create_challange_pow(target_path, auth_token):
    headers = get_headers(auth_token)
    json_data = {
        "target_path": target_path,
    }
    response = requests.post(
        "https://chat.deepseek.com/api/v0/chat/create_pow_challenge",
        cookies=COOKIE,
        headers=headers,
        json=json_data,
    ).json()
    return response["data"]["biz_data"]["challenge"]


def write_string_pow(text, alloc_func, memory, store):
    data = text.encode("utf-8")
    ptr = alloc_func(store, len(data))
    mem = memory.data_ptr(store)
    for i in range(len(data)):
        mem[ptr + i] = data[i]
    return ptr, len(data)


def find_pow_answer(challange_data):
    engine = wasmtime.Engine()
    with open(wasm_path, "rb") as f:
        wasm_bytes = f.read()
    module = wasmtime.Module(engine, wasm_bytes)
    store = wasmtime.Store(engine)
    linker = wasmtime.Linker(engine)
    instance = linker.instantiate(store, module)
    memory = instance.exports(store)["memory"]
    alloc_func = instance.exports(store)["alloc"]
    solve_func = instance.exports(store)["solve_pow"]
    ch_ptr, ch_len = write_string_pow(
        challange_data["challenge"], alloc_func, memory, store
    )
    salt_ptr, salt_len = write_string_pow(
        challange_data["salt"], alloc_func, memory, store
    )
    result = solve_func(
        store,
        ch_ptr,
        ch_len,
        salt_ptr,
        salt_len,
        challange_data["expire_at"],
        challange_data["difficulty"],
    )
    if result < 0:
        result = result + 0x10000000000000000
    return result if result != 0xFFFFFFFFFFFFFFFF else None


def solve_create_pow(target_path, auth_token):
    pow = create_challange_pow(target_path, auth_token)
    answer = find_pow_answer(pow)
    json_data = {
        "algorithm": "DeepSeekHashV1",
        "challenge": pow["challenge"],
        "salt": pow["salt"],
        "answer": answer,
        "signature": pow["signature"],
        "target_path": target_path,
    }
    return base64.b64encode(json.dumps(json_data).encode()).decode()


def fetch_chat_history(chat_id, auth_token):
    headers = get_headers(auth_token)
    url = (
        "https://chat.deepseek.com/api/v0/chat/history_messages?chat_session_id="
        + chat_id
    )
    data = requests.get(url, cookies=COOKIE, headers=headers).json()
    return data["data"]["biz_data"]["chat_messages"]


def create_new_chat(auth_token):
    headers = get_headers(auth_token)
    url = "https://chat.deepseek.com/api/v0/chat_session/create"
    data = requests.post(url, cookies=COOKIE, headers=headers).json()
    return data["data"]["biz_data"]["chat_session"]["id"]


def send_message(
    chat_id,
    auth_token,
    message,
    parent_message_id,
    thinking=False,
    search=False,
    model_type=None,
    file_ids=[],
):
    url = "https://chat.deepseek.com/api/v0/chat/completion"
    headers = get_headers(
        auth_token, solve_create_pow("/api/v0/chat/completion", auth_token)
    )
    json_data = {
        "chat_session_id": chat_id,
        "parent_message_id": parent_message_id,
        "model_type": model_type,
        "prompt": message,
        "ref_file_ids": file_ids,
        "thinking_enabled": False,
        "search_enabled": True,
        "preempt": False,
        "action": None,
    }
    with requests.post(
        url, cookies=COOKIE, headers=headers, json=json_data, stream=True
    ) as r:
        for line in r.iter_lines():
            if not line:
                continue
            decoded_line = line.decode("utf-8").strip()
            if decoded_line.startswith("event:"):
                continue

            if decoded_line.startswith("data: "):
                json_str = decoded_line[6:]
                if "FINISHED" in json_str or "BATCH" in json_str:
                    return
                data = json.loads(json_str)

                if "v" in data and "response" not in str(data["v"]):
                    yield data["v"]
                elif data.get("o") == "APPEND":
                    yield data["v"]
                elif "v" in data and "response" in data["v"]:
                    fragments = data["v"]["response"]["fragments"]
                    if fragments:
                        yield fragments[0]["content"]


def upload_file(file_bytes, file_name, file_content_type, auth_token):
    url = "https://chat.deepseek.com/api/v0/file/upload_file"
    file_size = len(file_bytes)
    pow_response = solve_create_pow("/api/v0/file/upload_file", auth_token)
    boundary = b"----WebKitFormBoundaryTB0pXOQR2RL219Hu"
    body_parts = [
        b"--" + boundary + b"\r\n",
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'.encode(
            "utf-8"
        ),
        f"Content-Type: {file_content_type}\r\n\r\n".encode("utf-8"),
        file_bytes,
        b"\r\n--" + boundary + b"--\r\n",
    ]
    reconstructed_body = b"".join(body_parts)
    headers = get_headers(auth_token, pow_response)
    headers.update(
        {
            "content-type": f"multipart/form-data; boundary={boundary.decode('utf-8')}",
            "x-file-size": str(file_size),
        }
    )
    response = requests.post(
        url, data=reconstructed_body, cookies=COOKIE, headers=headers
    ).json()
    file_id = response["data"]["biz_data"]["id"]
    yield ("uploaded", file_id)
    status = response["data"]["biz_data"]["status"]
    headers = get_headers(auth_token)
    while status in ["PENDING", "PARSING"]:
        yield ("uploaded", file_id)
        time.sleep(0.3)
        resp = requests.get(
            "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
            headers=headers,
            cookies=COOKIE,
        ).json()
        status = resp["data"]["biz_data"]["files"][0]["status"]

    if status == "SUCCESS":
        yield ("success", file_id)
    else:
        yield ("error", file_id)


def get_file_content(auth_token, file_id):
    headers = get_headers(auth_token)
    resp = requests.get(
        "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
        headers=headers,
        cookies=COOKIE,
    ).json()
    file_path = (
        "https://files.deepseeksvc.com/api"
        + resp["data"]["biz_data"]["files"][0]["signed_path"]
        + "&ty=r"
    )
    with requests.get(file_path, stream=True) as data:
        for chunk in data.iter_content(chunk_size=8192):
            if chunk:
                yield chunk


def list_api_keys():
    if not os.path.exists("api_keys.json"):
        with open("api_keys.json", "w") as f:
            f.write("{}")
            return {}
    return json.load(open("api_keys.json"))


def create_api_key(api_key):
    current_list = json.load(open("api_keys.json"))
    current_list[api_key] = "NOT_CREATED_YET"
    with open("api_keys.json", "w") as f:
        json.dump(current_list, f)


def get_api_key_session_id(api_key, auth_token):
    api_keys = json.load(open("api_keys.json"))
    if api_key not in api_keys.keys():
        return None
    session_id = api_keys[api_key]
    if session_id == "NOT_CREATED_YET":
        session_id = create_new_chat(auth_token)
        with open("api_keys.json", "w") as f:
            api_keys[api_key] = session_id
            json.dump(api_keys, f)
    return session_id
