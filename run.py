import asyncio
import os
import subprocess
import threading

import pgserver
import uvicorn

db = pgserver.get_server("./litellm_db")
raw_uri = db.get_uri()

socket_dir = raw_uri.split("host=")[1].split("&")[0]
unix_socket_path = os.path.join(socket_dir, ".s.PGSQL.5432")

TCP_PORT = 5432
db_uri = f"postgresql://postgres@127.0.0.1:{TCP_PORT}/postgres"
print(f"Database URI: {db_uri}")

os.environ["DATABASE_URL"] = db_uri
os.environ["LITELLM_MASTER_KEY"] = "dkseeker"


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
        server = await asyncio.start_server(handle_tcp_client, "127.0.0.1", TCP_PORT)
        print(f"Started bridge for pgserver on port {TCP_PORT}")
        async with server:
            await server.serve_forever()

    loop.run_until_complete(start_server())


bridge_thread = threading.Thread(target=run_bridge_thread, daemon=True)
bridge_thread.start()

import time

time.sleep(1)

from litellm.proxy.proxy_server import app, initialize, proxy_config

config_path = "config.yaml"


@app.on_event("startup")
async def startup_event():
    await initialize(config=config_path, master_key="dkseeker")
    await proxy_config.load_config(router=app.router, config_file_path=config_path)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=4000)
