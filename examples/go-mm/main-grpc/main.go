/*
 * RFQ Market Maker Main Flow (gRPC, v2 EIP-712 signing)
 *
 * Uses native gRPC MakerStream (bidirectional) instead of WebSocket.
 *
 * Wire payloads MUST carry `sign_mode: "v2"` — empty values are rejected
 * with `value of message.sign_mode must be one of "v1", "v2"`. The full
 * spec lives in PYTHON_BUILDING_GUIDE.md and rfq.inj.so/onboarding.html#sign.
 *
 * Flow:
 * 0. MM has already granted permissions to RFQ contract (see setup/setup.go)
 * 1. MM connects to gRPC MakerStream with maker metadata
 * 2. MM receives RFQ requests from the stream
 * 3. MM builds and signs quotes (v2 EIP-712 — see signQuoteV2 below)
 * 4. MM sends quotes back via the same stream with sign_mode="v2"
 * 5. MM receives quote_ack, quote_update, settlement_update events
 *
 * Prerequisites:
 *   Generate Go proto code first — see go-mm/proto/generate.sh
 */
package main

import (
	"context"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"log"
	"math/big"
	"os"
	"strconv"
	"time"

	"github.com/joho/godotenv"

	"github.com/cosmos/cosmos-sdk/types/bech32"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/ethereum/go-ethereum/common/hexutil"
	ethcrypto "github.com/ethereum/go-ethereum/crypto"

	ethsecp256k1 "github.com/InjectiveLabs/sdk-go/chain/crypto/ethsecp256k1"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	pb "mm-scripts-go/proto/injective_rfq_rpc"
)

const PING_INTERVAL = 10 * time.Second

func mustEnv(key string) string {
	v := os.Getenv(key)
	if v == "" {
		log.Fatalf("%s is not set", key)
	}
	return v
}

func trim0x(s string) string {
	if len(s) > 2 && s[:2] == "0x" {
		return s[2:]
	}
	return s
}

// --- EIP-712 v2 signing ----------------------------------------------------
// Mirrors the indexer reference byte-for-byte.

const eip712DomainType = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"

const signQuoteType = "SignQuote(string marketId,uint64 rfqId,address taker,uint8 takerDirection," +
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
	MinFillQuantity      string // pass "" or "0" when absent
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

	buf := make([]byte, 0, 32*16)
	buf = append(buf, ethcrypto.Keccak256([]byte(signQuoteType))...)
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

func sendQuote(
	stream pb.InjectiveRfqRPC_MakerStreamClient,
	req *pb.RFQRequestType,
	price float64,
	makerAddr string,
	mmPK string,
	chainID string,
	contractAddr string,
	evmChainID uint64,
) error {
	expiryMs := uint64(time.Now().Add(20 * time.Second).UnixMilli())
	priceStr := fmt.Sprintf("%.1f", price)

	sig, err := signQuoteV2(signQuoteInput{
		PrivateKey:           mmPK,
		EvmChainID:           evmChainID,
		ContractAddress:      contractAddr,
		MarketID:             req.MarketId,
		RfqID:                req.RfqId,
		Taker:                req.RequestAddress,
		Direction:            req.Direction,
		TakerMargin:          req.Margin,
		TakerQuantity:        req.Quantity,
		Maker:                makerAddr,
		MakerSubaccountNonce: 0,
		MakerQuantity:        req.Quantity,
		MakerMargin:          req.Margin,
		Price:                priceStr,
		ExpiryKind:           0, // timestamp
		ExpiryValue:          expiryMs,
		MinFillQuantity:      "",
	})
	if err != nil {
		return fmt.Errorf("sign error: %w", err)
	}

	quote := &pb.RFQQuoteType{
		ChainId:         chainID,
		ContractAddress: contractAddr,
		RfqId:           req.RfqId,
		MarketId:        req.MarketId,
		TakerDirection:  req.Direction,
		Margin:          req.Margin,
		Quantity:        req.Quantity,
		Price:           priceStr,
		Expiry:          &pb.RFQExpiryType{Timestamp: expiryMs},
		Maker:           makerAddr,
		Taker:           req.RequestAddress,
		Signature:       sig,
		SignMode:        "v2", // required by indexer
	}

	fmt.Printf("\n📤 Sending quote (price=%s)\n", priceStr)
	return stream.Send(&pb.MakerStreamStreamingRequest{
		MessageType: "quote",
		Quote:       quote,
	})
}

func main() {
	_ = godotenv.Load()

	grpcEndpoint := mustEnv("GRPC_ENDPOINT")
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

	// Derive maker address (Injective bech32)
	mmPkDecoded, err := hex.DecodeString(trim0x(mmPK))
	if err != nil {
		log.Fatal(err)
	}

	pk := ethsecp256k1.PrivKey{Key: mmPkDecoded}
	sdk.GetConfig().SetBech32PrefixForAccount("inj", "inj")
	makerAddr := sdk.AccAddress(pk.PubKey().Address().Bytes()).String()

	fmt.Println("🔌 Connecting to gRPC MakerStream...")
	fmt.Println("   Endpoint:", grpcEndpoint)
	fmt.Println("   Maker:   ", makerAddr)

	// Dial gRPC
	var dialOpts []grpc.DialOption
	if grpcEndpoint == "localhost" || grpcEndpoint[:4] == "127." {
		dialOpts = append(dialOpts, grpc.WithTransportCredentials(insecure.NewCredentials()))
	} else {
		dialOpts = append(dialOpts, grpc.WithTransportCredentials(credentials.NewTLS(nil)))
	}

	conn, err := grpc.NewClient(grpcEndpoint, dialOpts...)
	if err != nil {
		log.Fatalf("grpc dial: %v", err)
	}
	defer conn.Close()

	client := pb.NewInjectiveRfqRPCClient(conn)

	// Set maker metadata
	ctx := metadata.NewOutgoingContext(context.Background(), metadata.Pairs(
		"maker_address", makerAddr,
		"subscribe_to_quotes_updates", "true",
		"subscribe_to_settlement_updates", "true",
	))

	stream, err := client.MakerStream(ctx)
	if err != nil {
		log.Fatalf("MakerStream: %v", err)
	}

	// Send initial ping (first message carries data alongside HEADERS frame)
	if err := stream.Send(&pb.MakerStreamStreamingRequest{MessageType: "ping"}); err != nil {
		log.Fatalf("initial ping: %v", err)
	}

	// Background ping loop
	go func() {
		ticker := time.NewTicker(PING_INTERVAL)
		defer ticker.Stop()
		for range ticker.C {
			if err := stream.Send(&pb.MakerStreamStreamingRequest{MessageType: "ping"}); err != nil {
				log.Printf("ping error: %v", err)
				return
			}
		}
	}()

	fmt.Println("📡 MM connected — listening for RFQ requests...")
	fmt.Println()

	// Read loop
	for {
		resp, err := stream.Recv()
		if err != nil {
			log.Fatalf("MakerStream recv: %v", err)
		}

		switch resp.MessageType {
		case "pong":
			// keep-alive ack

		case "request":
			req := resp.Request
			fmt.Printf("\n📩 RFQ request: RFQ#%d market=%s\n", req.RfqId, req.MarketId)
			fmt.Printf("   direction=%s margin=%s qty=%s\n", req.Direction, req.Margin, req.Quantity)

			// Demo pricing ladder
			for _, p := range []float64{1.3, 1.4, 1.5} {
				if err := sendQuote(stream, req, p, makerAddr, mmPK, chainID, contractAddr, evmChainID); err != nil {
					log.Printf("sendQuote error: %v", err)
				}
			}

		case "quote_ack":
			ack := resp.QuoteAck
			fmt.Printf("📬 Quote ACK: rfq_id=%d status=%s\n", ack.RfqId, ack.Status)

		case "quote_update":
			qu := resp.ProcessedQuote
			fmt.Printf("📊 Quote update: rfq_id=%d status=%s\n", qu.RfqId, qu.Status)

		case "settlement_update":
			s := resp.Settlement
			fmt.Printf("⚖️  Settlement: rfq_id=%d cid=%s\n", s.RfqId, s.Cid)
			for _, q := range s.Quotes {
				fmt.Printf("   quote: maker=%s price=%s status=%s\n", q.Maker, q.Price, q.Status)
			}

		case "error":
			e := resp.Error
			log.Printf("❌ Stream error: %s: %s", e.Code, e.Message_)

		default:
			fmt.Printf("Unknown message type: %s\n", resp.MessageType)
		}
	}
}
