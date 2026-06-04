print(
    "Go to chrome/firefox browser and login with your deepseek account and let it load properly."
)
print(
    "After that press CTRL+SHIFT+I and click on console and paste the output of following command from it without quotes here:"
)
print('Command to run: JSON.parse(localStorage.getItem("userToken")).value')
auth_token = input("Enter output of above command without quotes: ")
with open("auth_token.txt", "w") as f:
    f.write(auth_token)

print("Auth token stored successfully.")
