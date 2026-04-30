# Market maker RFQ Integration

This guide explains how a market maker (MM) integrates with the RFQ system: permission setup, listening for RFQs, and responding with signed quotes.

## 1. Permission Setup (One-Time)

a. Prepare .env from ../env.example

b. Example: `mm-scripts-go/setup/setup.go`

### Step 1: Grant permissions to the RFQ contract

The MM must grant the RFQ contract the following authz permissions.
These permissions allow the contract to settle trades and move funds on behalf of the MM.

```
"/injective.exchange.v2.MsgPrivilegedExecuteContract",
"/cosmos.bank.v1beta1.MsgSend",
```

### Step 2: Contract admin whitelists the MM

After permissions are granted, the RFQ contract admin enables (whitelists) the MM address.
Only whitelisted MMs can submit valid quotes.

## 2. Listening for RFQ Requests

Example: `mm-scripts-go/main/main.go`

### Step 1: Subscribe to RFQ requests via WebSocket

```
Each RfqRequest contains:
- Market ID
- Direction (LONG / SHORT)
- Margin & quantity
- Taker address
- RFQ ID and expiry
```

### Step 2: Prepare and sign the quote

To respond, the MM builds a quote and signs it with the MM’s private key.

**Important**

The contract only accepts quotes signed by the registered maker address

Signature format is secp256k1 (65 bytes). The v2 EIP-712 examples serialize
`v` as compact y-parity (`0`/`1`), matching ws-client.

Payload is hashed with keccak256

Once signed, the quote is sent back to the RFQ system via WebSocket.


```
// following code shows how MM signs the quote
// contract only accept the quote if signature comes from the "marker" address
func signQuote(signQuote SignQuote, privKeyHex string) (string, error) {
	payload, err := json.Marshal(signQuote)
	if err != nil {
		return "", err
	}

	fmt.Println("signed payload:", string(payload))

	hash := ethcrypto.Keccak256(payload)

	privKeyHex = trim0x(privKeyHex)
	privKey, err := ethcrypto.HexToECDSA(privKeyHex)
	if err != nil {
		return "", err
	}

	sig, err := ethcrypto.Sign(hash, privKey)
	if err != nil {
		return "", err
	}

	// Legacy JSON path uses Ethereum-style recovery id.
	sig[64] += 27

	return hexutil.Encode(sig), nil
}
```

**NOTE**

At the moment, closing a position still requires margin.
When quoting a close, the MM should include margin in the quote.
The contract will temporarily borrow this margin and return it within the same transaction.

### Step 3: Send the quote back via WebSocket

Once signed, the quote is sent back to the RFQ system via WebSocket.

```
func sendQuote(
	ws *websocket.Conn,
	req RfqRequest,
	price float64,
	makerAddr string,
	mmPK string,
) error {

	// ...
	// prepare message and send the quote to user
	msg := map[string]any{
		"jsonrpc": "2.0",
		"method":  "quote",
		"id":      time.Now().UnixMilli(),
		"params": map[string]any{
			"quote": quote,
		},
	}

	
	bz, _ := json.MarshalIndent(msg, "", "  ")
	fmt.Println("\n📤 Sending quote")
	fmt.Println(string(bz))

	return ws.WriteMessage(websocket.TextMessage, bz)
}
```
