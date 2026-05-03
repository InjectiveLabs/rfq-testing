# Install dependencies

```
pip3 install injective-py eth-keys websockets eth-account python-dotenv
```

# Setup


```
# create & tweak new env if haven't
cp ./env.example ./env

# run setup script
python3 python-mm/setup.py
```

# MM main script

```
# run main script (assume we have .env file from above step already)
python3 python-mm/main-grpc.py
```
