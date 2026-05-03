/*
 * RFQ Market Maker Main Flow (v2 EIP-712 signing)
 *
 * Wire payloads MUST carry `sign_mode: "v2"` — empty values are rejected
 * with `value of message.sign_mode must be one of "v1", "v2"`. The full
 * spec lives in PYTHON_BUILDING_GUIDE.md and rfq.inj.so/onboarding.html#sign.
 *
 * Flow:
 * 0. MM has already granted permissions to RFQ contract
 * 1. MM connects to WebSocket & subscribes to RFQ requests
 * 2. MM receives RFQ request from retail
 * 3. MM builds quotes, for blind quote, MM will choose nonce
 * 4. MM signs quotes (v2 EIP-712 — see signQuoteV2 below)
 * 5. MM sends quotes back via WebSocket with sign_mode="v2"
 */
package main

import (
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"math/big"
	"net/url"
	"os"
	"strconv"
	"time"

	"github.com/gorilla/websocket"
	"github.com/joho/godotenv"

	"github.com/cosmos/cosmos-sdk/types/bech32"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/ethereum/go-ethereum/common/hexutil"
	ethcrypto "github.com/ethereum/go-ethereum/crypto"

	ethsecp256k1 "github.com/InjectiveLabs/sdk-go/chain/crypto/ethsecp256k1"
)

const WS_URL = "ws://localhost:4464/ws"
const INJUSDT_MARKET_ID = "0x7cc8b10d7deb61e744ef83bdec2bbcf4a056867e89b062c6a453020ca82bd4e4"

func mustEnv(key string) string {
	v := os.Getenv(key)
	if v == "" {
		log.Fatalf("%s is not set", key)
	}
	return v
}

type RfqRequest struct {
	RfqID       uint64 `json:"rfq_id"`
	MarketID    string `json:"market_id"`
	Direction   int    `json:"direction"`
	Margin      string `json:"margin"`
	Quantity    string `json:"quantity"`
	WorstPrice  string `json:"worst_price"`
	RequestAddr string `json:"request_address"`
	Expiry      uint64 `json:"expiry"`
}

type Quote struct {
	ChainID         string  `json:"chain_id"`
	ContractAddress string  `json:"contract_address"`
	MarketID        string  `json:"market_id"`
	RfqID           uint64  `json:"rfq_id"`
	Direction       int     `json:"direction"`
	Margin          string  `json:"margin"`
	Quantity        string  `json:"quantity"`
	Price           string  `json:"price"`
	Expiry          uint64  `json:"expiry"`
	Maker           string  `json:"maker,omitempty"`
	Taker           string  `json:"taker,omitempty"`
	Signature       string  `json:"signature"`
	SignMode        string  `json:"sign_mode"`            // required by indexer ("v2" for everything this script signs)
	EvmChainID      uint64  `json:"evm_chain_id"`
	Nonce           *uint64 `json:"nonce,omitempty"`
}

// --- EIP-712 v2 signing ----------------------------------------------------
//
// Mirrors the indexer's reference implementation byte-for-byte. The protocol
// uses a custom typed-data layout (NOT eth_signTypedData_v4): a fixed
// EIP712Domain separator plus a hand-rolled SignQuote typeHash. Each field
// is encoded into a 32-byte word. Decimals are hashed as keccak256(utf8(s)),
// so the wire price MUST equal the signed price byte-for-byte.

const eip712DomainType = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"

const signQuoteType = "SignQuote(uint64 evmChainId,string marketId,uint64 rfqId,address taker,uint8 takerDirection," +
	"string takerMargin,string takerQuantity,address maker,uint32 makerSubaccountNonce," +
	"string makerQuantity,string makerMargin,string price,uint8 expiryKind," +
	"uint64 expiryValue,string minFillQuantity,uint8 bindingKind)"

const eip712DomainName = "RFQ"
const eip712DomainVersion = "1"

func bech32ToEvm(addr string) ([20]byte, error) {
	hrp, raw, err := bech32.DecodeAndConvert(addr)
	if err != nil {
		return [20]byte{}, fmt.Errorf("decode bech32 %q: %w", addr, err)
	}
	if hrp != "inj" {
		return [20]byte{}, fmt.Errorf("expected hrp 'inj', got %q", hrp)
	}
	if len(raw) != 20 {
		return [20]byte{}, fmt.Errorf("expected 20 bytes, got %d", len(raw))
	}
	var out [20]byte
	copy(out[:], raw)
	return out, nil
}

func encU8(v uint8) []byte    { b := make([]byte, 32); b[31] = v; return b }
func encU32(v uint32) []byte  { b := make([]byte, 32); binary.BigEndian.PutUint32(b[28:], v); return b }
func encU64(v uint64) []byte  { b := make([]byte, 32); binary.BigEndian.PutUint64(b[24:], v); return b }
func encAddr(a [20]byte) []byte { b := make([]byte, 32); copy(b[12:], a[:]); return b }
func encString(s string) []byte { return ethcrypto.Keccak256([]byte(s)) }

func chainIDWord(evmChainID uint64) []byte {
	out := make([]byte, 32)
	new(big.Int).SetUint64(evmChainID).FillBytes(out)
	return out
}

func domainSeparator(evmChainID uint64, contractBech32 string) ([]byte, error) {
	addr20, err := bech32ToEvm(contractBech32)
	if err != nil {
		return nil, err
	}
	buf := make([]byte, 0, 32*5)
	buf = append(buf, ethcrypto.Keccak256([]byte(eip712DomainType))...)
	buf = append(buf, encString(eip712DomainName)...)
	buf = append(buf, encString(eip712DomainVersion)...)
	buf = append(buf, chainIDWord(evmChainID)...)
	buf = append(buf, encAddr(addr20)...)
	return ethcrypto.Keccak256(buf), nil
}

type signQuoteInput struct {
	PrivateKey           string
	EvmChainID           uint64
	ContractAddress      string // bech32
	MarketID             string
	RfqID                uint64
	Taker                string // bech32
	Direction            string // "long" or "short"
	TakerMargin          string
	TakerQuantity        string
	Maker                string // bech32
	MakerSubaccountNonce uint32
	MakerQuantity        string
	MakerMargin          string
	Price                string
	ExpiryKind           uint8 // 0 = timestamp_ms, 1 = block_height
	ExpiryValue          uint64
	MinFillQuantity      string // pass "0" when absent
}

func signQuoteV2(in signQuoteInput) (string, error) {
	takerAddr, err := bech32ToEvm(in.Taker)
	if err != nil {
		return "", fmt.Errorf("taker: %w", err)
	}
	makerAddr, err := bech32ToEvm(in.Maker)
	if err != nil {
		return "", fmt.Errorf("maker: %w", err)
	}
	var directionByte uint8
	switch in.Direction {
	case "long":
		directionByte = 0
	case "short":
		directionByte = 1
	default:
		return "", fmt.Errorf("invalid direction %q", in.Direction)
	}

	mfq := in.MinFillQuantity
	if mfq == "" {
		mfq = "0"
	}

	buf := make([]byte, 0, 32*17)
	buf = append(buf, ethcrypto.Keccak256([]byte(signQuoteType))...)
	buf = append(buf, encU64(in.EvmChainID)...)
	buf = append(buf, encString(in.MarketID)...)
	buf = append(buf, encU64(in.RfqID)...)
	buf = append(buf, encAddr(takerAddr)...)
	buf = append(buf, encU8(directionByte)...)
	buf = append(buf, encString(in.TakerMargin)...)
	buf = append(buf, encString(in.TakerQuantity)...)
	buf = append(buf, encAddr(makerAddr)...)
	buf = append(buf, encU32(in.MakerSubaccountNonce)...)
	buf = append(buf, encString(in.MakerQuantity)...)
	buf = append(buf, encString(in.MakerMargin)...)
	buf = append(buf, encString(in.Price)...)
	buf = append(buf, encU8(in.ExpiryKind)...)
	buf = append(buf, encU64(in.ExpiryValue)...)
	buf = append(buf, encString(mfq)...)
	buf = append(buf, encU8(1)...) // bindingKind = 1 (taker-specific)

	msgHash := ethcrypto.Keccak256(buf)
	domain, err := domainSeparator(in.EvmChainID, in.ContractAddress)
	if err != nil {
		return "", err
	}
	prefixed := append([]byte{0x19, 0x01}, append(domain, msgHash...)...)
	digest := ethcrypto.Keccak256(prefixed)

	privKey, err := ethcrypto.HexToECDSA(trim0x(in.PrivateKey))
	if err != nil {
		return "", err
	}
	sig, err := ethcrypto.Sign(digest, privKey)
	if err != nil {
		return "", err
	}
	return hexutil.Encode(sig), nil
}

func trim0x(s string) string {
	if len(s) > 2 && s[:2] == "0x" {
		return s[2:]
	}
	return s
}

func sendQuote(
	ws *websocket.Conn,
	req *RfqRequest,
	price float64,
	makerAddr string,
	mmPK string,
	chainID string,
	contractAddress string,
	evmChainID uint64,
) error {
	if req == nil {
		// Blind quotes don't bind to a specific taker; v2 signing requires the
		// taker address in the digest. The blind path is not supported here.
		return fmt.Errorf("blind-quote v2 signing not implemented in this example")
	}

	expiry := uint64(time.Now().Add(8 * time.Second).UnixMilli())
	rfqId := req.RfqID
	margin := req.Margin
	qty, _ := strconv.ParseFloat(req.Quantity, 64)
	quantity := fmt.Sprintf("%d", int(qty/2))
	marketId := req.MarketID
	takerAddress := req.RequestAddr

	// req.Direction is the TAKER's direction (0=long, 1=short).
	// MM quotes the opposite side; the digest binds the taker's direction.
	mmDirection := 1
	if req.Direction == 1 {
		mmDirection = 0
	}
	takerDirectionStr := "long"
	if req.Direction == 1 {
		takerDirectionStr = "short"
	}

	priceStr := fmt.Sprintf("%.1f", price)

	sig, err := signQuoteV2(signQuoteInput{
		PrivateKey:           mmPK,
		EvmChainID:           evmChainID,
		ContractAddress:      contractAddress,
		MarketID:             marketId,
		RfqID:                rfqId,
		Taker:                takerAddress,
		Direction:            takerDirectionStr,
		TakerMargin:          req.Margin,
		TakerQuantity:        req.Quantity,
		Maker:                makerAddr,
		MakerSubaccountNonce: 0,
		MakerQuantity:        quantity,
		MakerMargin:          margin,
		Price:                priceStr,
		ExpiryKind:           0, // timestamp
		ExpiryValue:          expiry,
		MinFillQuantity:      "",
	})
	if err != nil {
		return err
	}

	quote := Quote{
		ChainID:         chainID,
		ContractAddress: contractAddress,
		RfqID:           rfqId,
		MarketID:        marketId,
		Direction:       mmDirection,
		Margin:          margin,
		Quantity:        quantity,
		Price:           priceStr,
		Expiry:          expiry,
		Maker:           makerAddr,
		Taker:           takerAddress,
		Signature:       sig,
		SignMode:        "v2",
		EvmChainID:      evmChainID,
	}

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

func main() {
	_ = godotenv.Load()

	mmPK := mustEnv("MM_PRIVATE_KEY")
	contractAddr := mustEnv("CONTRACT_ADDRESS")
	chainID := mustEnv("CHAIN_ID")
	// EVM_CHAIN_ID for the EIP-712 v2 domain separator (1439 testnet, 1776 mainnet).
	evmChainID := uint64(1439)
	if v := os.Getenv("EVM_CHAIN_ID"); v != "" {
		parsed, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			log.Fatalf("EVM_CHAIN_ID must be a uint64: %v", err)
		}
		evmChainID = parsed
	}

	// derive maker address (Injective bech32)
	mmPkDecoded, err := hex.DecodeString(trim0x(mmPK))
	if err != nil {
		panic(err)
	}

	pk := ethsecp256k1.PrivKey{
		Key: mmPkDecoded,
	}
	if err != nil {
		panic(err)
	}
	sdk.GetConfig().SetBech32PrefixForAccount("inj", "inj")
	makerAddr := sdk.AccAddress(pk.PubKey().Address().Bytes()).String()

	u, _ := url.Parse(WS_URL)
	ws, _, err := websocket.DefaultDialer.Dial(u.String(), nil)
	if err != nil {
		panic(err)
	}
	defer ws.Close()

	fmt.Println("🔌 MM WebSocket connected")

	// subscribe
	sub := map[string]any{
		"jsonrpc": "2.0",
		"method":  "subscribe",
		"id":      1,
		"params": map[string]any{
			"query": map[string]any{
				"stream": "request",
				"args": map[string]any{
					"market_ids": []string{INJUSDT_MARKET_ID},
				},
			},
		},
	}
	ws.WriteJSON(sub)

	fmt.Println("📡 Subscribed to RFQ request stream")

	for {
		_, data, err := ws.ReadMessage()
		if err != nil {
			log.Println("❌ WebSocket error:", err)
			return
		}

		var msg struct {
			Result struct {
				Request *RfqRequest `json:"request"`
			} `json:"result"`
		}

		if err := json.Unmarshal(data, &msg); err != nil {
			continue
		}

		if msg.Result.Request == nil {
			continue
		}

		req := msg.Result.Request
		fmt.Println("\n📩 RFQ request received")
		fmt.Printf("%+v\n", req)

		prices := []float64{1.2, 1.4}
		for _, p := range prices {
			if err := sendQuote(ws, req, p, makerAddr, mmPK, chainID, contractAddr, evmChainID); err != nil {
				log.Println("sendQuote error:", err)
			}
		}

		// Blind-quote v2 signing is not implemented in this example
		// (the digest binds a specific taker address). Skipping the blind path.
		if err := sendQuote(ws, nil, 1.3, makerAddr, mmPK, chainID, contractAddr, evmChainID); err != nil {
			log.Println("send blind quote error:", err)
		}
	}
}
