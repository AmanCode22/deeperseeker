import asyncio
import os
import threading
from contextlib import asynccontextmanager
from typing import Optional

import pgserver
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from functions import get_file_content, upload_file
from litellm.proxy.proxy_server import app, initialize, user_api_key_cache

security_scheme = HTTPBearer()

db = pgserver.get_server("./litellm_db")
raw_uri = db.get_uri()

socket_dir = raw_uri.split("host=")[1].split("&")[0]
unix_socket_path = os.path.join(socket_dir, ".s.PGSQL.5432")


db_uri = f"postgresql://postgres@127.0.0.1:5432/postgres"
print(f"Database URI: {db_uri}")

load_dotenv()
os.environ["DATABASE_URL"] = db_uri


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def handle_tcp_client(tcp_reader, tcp_writer):
    try:
        unix_reader, unix_writer = await asyncio.open_unix_connection(unix_socket_path)
        await asyncio.gather(
            pipe(tcp_reader, unix_writer),
            pipe(unix_reader, tcp_writer),
            return_exceptions=True,
        )
    except Exception:
        try:
            tcp_writer.close()
        except Exception:
            pass


def run_bridge_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def start_server():
        server = await asyncio.start_server(handle_tcp_client, "127.0.0.1", 5432)
        print(f"Started bridge for pgserver on port 5432")
        async with server:
            await server.serve_forever()

    loop.run_until_complete(start_server())


bridge_thread = threading.Thread(target=run_bridge_thread, daemon=True)
bridge_thread.start()


@app.api_route("/customfiles/files", methods=["GET", "POST"])
async def files_api_ovveride(
    request: Request,
    file: UploadFile = File(...),
    purpose: Optional[str] = Form(None),
    anthropic_beta: Optional[str] = Header(None, alias="anthropic-beta"),
):
    if request.method == "GET":
        return {"object": "list", "data": []}
    else:
        file_bytes = await file.read()
        is_anthropic = False
        filename = file.filename or "unknown_file.txt"
        mime_type = file.content_type or "application/octet-stream"
        if not os.path.exists("auth_token.txt"):
            print("auth_token.txt not exists, run add_auth_token.py first.")
            os._exit(0)
        with open("auth_token.txt") as f:
            auth_token = f.read().strip()
        for i in upload_file(file_bytes, filename, mime_type, auth_token):
            if i[0] == "success":
                if (
                    anthropic_beta is not None
                    or "anthropic" in request.headers.get("user-agent", "").lower()
                ):
                    return {
                        "id": i[1]["file_id"],
                        "type": "file",
                        "filename": filename,
                        "mime_type": mime_type,
                        "size_bytes": i[1]["size"],
                        "created_at": i[1]["anthropic_timestamp"],
                        "downloadable": True,
                    }
                else:
                    return {
                        "id": i[1]["file_id"],
                        "object": "file",
                        "bytes": i[1]["size"],
                        "created_at": i[1]["openai_timestamp"],
                        "filename": filename,
                        "purpose": purpose or "fine-tune",
                    }
    return {
        "type": "error",
        "error": {
            "type": "internal_error",
            "message": "An internal server error occurred.",
            "param": None,
            "code": "internal_error",
        },
    }


@app.api_route(
    "/customfiles/files/{file_id}/content",
    status_code=status.HTTP_200_OK,
    methods=["GET"],
)
async def files_content_ovveride(file_id: str):
    if not os.path.exists("auth_token.txt"):
        print("auth_token.txt not exists, run add_auth_token.py first.")
        os._exit(0)
    with open("auth_token.txt") as f:
        auth_token = f.read().strip()
    file_data_generator = get_file_content(auth_token, file_id)
    return StreamingResponse(file_data_generator, media_type=next(file_data_generator))


@app.middleware("http")
async def intercept_file_content_endpoint(request: Request, call_next):
    path = request.url.path
    if path.startswith("/v1/files/") and path.endswith("/content"):
        parts = path.split("/")
        if len(parts) >= 4:
            file_id = parts[3]
            request.scope["path"] = f"/customfiles/files/{file_id}/content"
    elif path == "/v1/files/":
        request.scope["path"] = f"/customfiles/files"

    response = await call_next(request)
    return response


original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def custom_lifespan(fastapi_app: FastAPI):
    print("Running litellm server")

    await initialize(config="config.yaml")

    async with original_lifespan(fastapi_app) as state:
        yield state


app.router.lifespan_context = custom_lifespan

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=4000)
