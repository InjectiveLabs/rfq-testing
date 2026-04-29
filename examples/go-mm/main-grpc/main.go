/*
 * !!! v1 SIGNING — NEEDS PORT TO v2 (EIP-712) !!!
 * As of 2026-04-29 the indexer rejects empty `sign_mode`. v2 is the
 * rfq-testing standard. Canonical v2 reference (port from these):
 *   - injective-indexer service/rfq/signature/eip712.go (Go)
 *   - rfq-testing src/rfq_test/crypto/eip712.py
 *   - PYTHON_BUILDING_GUIDE.md
 *
 * RFQ Market Maker Main Flow (gRPC)
 *
 * Uses native gRPC MakerStream (bidirectional) instead of WebSocket.
 *
 * Flow:
 * 0. MM has already granted permissions to RFQ contract (see setup/setup.go)
 * 1. MM connects to gRPC MakerStream with maker metadata
 * 2. MM receives RFQ requests from the stream
 * 3. MM builds and signs quotes
 * 4. MM sends quotes back via the same stream
 * 5. MM receives quote_ack, quote_update, settlement_update events
 *
 * Prerequisites:
 *   Generate Go proto code first — see go-mm/proto/generate.sh
 */
package main

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/joho/godotenv"

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

// SignQuote is the compact signing struct that matches the contract.
// Field order: c, ca, mi, id, t, td, tm, tq, m, ms, mq, mm, p, e[, mfq]
type SignQuote struct {
	ChainID              string      `json:"c"`
	ContractAddress      string      `json:"ca"`
	MarketIndex          string      `json:"mi"`
	RfqId                uint64      `json:"id"`
	Taker                string      `json:"t,omitempty"`
	TakerDirection       string      `json:"td"`
	TakerMargin          string      `json:"tm,omitempty"`
	TakerQuantity        string      `json:"tq,omitempty"`
	Maker                string      `json:"m"`
	MakerSubaccountNonce int         `json:"ms"`
	MakerQuantity        string      `json:"mq"`
	MakerMargin          string      `json:"mm"`
	Price                string      `json:"p"`
	Expiry               interface{} `json:"e"`
	MinFillQuantity      string      `json:"mfq,omitempty"`
}

type ExpiryTS struct {
	TS uint64 `json:"ts"`
}

type ExpiryH struct {
	H uint64 `json:"h"`
}

func toSignQuote(req *pb.RFQRequestType, chainID, contractAddr, makerAddr string, price string, expiryMs uint64) SignQuote {
	return SignQuote{
		ChainID:              chainID,
		ContractAddress:      contractAddr,
		MarketIndex:          req.MarketId,
		RfqId:                req.RfqId,
		Taker:                req.RequestAddress,
		TakerDirection:       req.Direction,
		TakerMargin:          req.Margin,
		TakerQuantity:        req.Quantity,
		Maker:                makerAddr,
		MakerSubaccountNonce: 0,
		MakerQuantity:        req.Quantity,
		MakerMargin:          req.Margin,
		Price:                price,
		Expiry:               ExpiryTS{TS: expiryMs},
	}
}

func signQuotePayload(sq SignQuote, privKeyHex string) (string, error) {
	payload, err := json.Marshal(sq)
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

func sendQuote(
	stream pb.InjectiveRfqRPC_MakerStreamClient,
	req *pb.RFQRequestType,
	price float64,
	makerAddr string,
	mmPK string,
	chainID string,
	contractAddr string,
) error {
	expiryMs := uint64(time.Now().Add(20 * time.Second).UnixMilli())
	priceStr := fmt.Sprintf("%.1f", price)

	sq := toSignQuote(req, chainID, contractAddr, makerAddr, priceStr, expiryMs)
	sig, err := signQuotePayload(sq, mmPK)
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

	fmt.Println("📡 MM connected — listening for RFQ requests...\n")

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
				if err := sendQuote(stream, req, p, makerAddr, mmPK, chainID, contractAddr); err != nil {
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
