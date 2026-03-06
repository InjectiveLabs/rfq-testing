/*
 * RFQ Market Maker Main Flow
 *
 * Flow:
 * 0. MM has already granted permissions to RFQ contract
 * 1. MM connects to WebSocket & subscribes to RFQ requests
 * 2. MM receives RFQ request from retail
 * 3. MM builds quotes, for blind quote, MM will choose nonce
 * 4. MM signs quotes (see example)
 * 5. MM sends quotes back via WebSocket
 */
package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/url"
	"os"
	"strconv"
	"time"

	"github.com/gorilla/websocket"
	"github.com/joho/godotenv"

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
	Nonce           *uint64 `json:"nonce,omitempty"`
}

// compact signing struct (MUST MATCH CONTRACT)
type SignQuote struct {
	ChainID         string `json:"c"`
	ContractAddress string `json:"ca"`
	MarketIndex     string `json:"mi"`
	RfqId           uint64 `json:"id"`
	Taker           string `json:"t,omitempty"`
	TakerDirection  string `json:"td"`
	TakerMargin     string `json:"tm,omitempty"`
	TakerQuantity   string `json:"tq,omitempty"`
	Maker           string `json:"m"`
	MakerQuantity   string `json:"mq"`
	MakerMargin     string `json:"mm"`
	Price           string `json:"p"`
	Expiry          uint64 `json:"e"`
}

func toSignQuote(req *RfqRequest, q Quote) SignQuote {
	// mm quote = long => taker direction = short
	td := "short"
	if q.Direction == 1 {
		td = "long"
	}

	rfqId := q.RfqID
	if q.Nonce != nil {
		rfqId = *q.Nonce
	}

	sign := SignQuote{
		ChainID:         q.ChainID,
		ContractAddress: q.ContractAddress,
		MarketIndex:     q.MarketID,
		RfqId:           rfqId, // IMPORTANT: for blind quote this must already be nonce
		TakerDirection:  td,
		Maker:           q.Maker,
		MakerQuantity:   q.Quantity,
		MakerMargin:     q.Margin,
		Price:           q.Price,
		Expiry:          q.Expiry,
	}

	// Explicit quote
	if req != nil {
		sign.Taker = req.RequestAddr
		sign.TakerMargin = req.Margin
		sign.TakerQuantity = req.Quantity
	}
	return sign
}

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

	// ethers-style signature (65 bytes, v = 27/28)
	sig[64] += 27

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
) error {
	expiry := uint64(time.Now().Add(8 * time.Second).UnixMilli())
	margin, quantity := "", ""
	nonce := (*uint64)(nil)
	rfqId := uint64(0)
	takerAddress := ""
	marketId := ""
	direction := 1

	// MM decides margin, quantity & side
	if req == nil {
		margin = "100"
		quantity = "5"
		n := uint64(time.Now().UnixMilli()) // insert maker's nonce, we don't have taker's rfq id so leave it empty
		nonce = &n
		marketId = INJUSDT_MARKET_ID
	} else {
		rfqId = uint64(req.RfqID)
		margin = req.Margin
		qty, _ := strconv.ParseFloat(req.Quantity, 64)
		quantity = fmt.Sprintf("%d", int(qty/2))
		marketId = req.MarketID
		takerAddress = req.RequestAddr

		if req.Direction == 1 {
			direction = 0
		}
	}

	quote := Quote{
		ChainID:         chainID,
		ContractAddress: contractAddress,
		RfqID:           uint64(rfqId),
		MarketID:        marketId,
		Direction:       direction,
		Margin:          margin,
		Quantity:        quantity,
		Price:           fmt.Sprintf("%.1f", price),
		Expiry:          expiry,
		Maker:           makerAddr,
		Taker:           takerAddress,
		Nonce:           nonce,
	}

	sig, err := signQuote(toSignQuote(req, quote), mmPK)
	if err != nil {
		return err
	}

	quote.Signature = sig

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
			if err := sendQuote(ws, req, p, makerAddr, mmPK, chainID, contractAddr); err != nil {
				log.Println("sendQuote error:", err)
			}
		}

		if err := sendQuote(ws, nil, 1.3, makerAddr, mmPK, chainID, contractAddr); err != nil {
			log.Println("send blind quote error:", err)
		}
	}
}
