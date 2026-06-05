import asyncio
import os
import threading
from contextlib import asynccontextmanager

import pgserver
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from litellm.proxy.proxy_server import app, initialize, proxy_config, user_api_key_cache

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


@app.post("/customfile")
async def files_api_ovveride(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
):
    api_key = credentials.credentials
    key_row = await user_api_key_cache.async_get(api_key)
    return key_row


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
