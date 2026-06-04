import os
import urllib.parse

import pgserver

db = pgserver.get_server("./litellm_db")
raw_uri = db.get_uri()
print(f"Original pgserver URI: {raw_uri}")


socket_dir = raw_uri.split("host=")[1].split("&")[0]
encoded_socket_dir = urllib.parse.quote_plus(socket_dir)


prisma_uri = f"postgresql://postgres@localhost/postgres?host={encoded_socket_dir}"
print(f"Formatted Prisma URI:  {prisma_uri}")

os.environ["DATABASE_URL"] = prisma_uri


print("Generating prisma binary in db....")
os.system("prisma generate")
os.system("prisma db push --accept-data-loss")
print("Generated prisma binary in db successfully!")
