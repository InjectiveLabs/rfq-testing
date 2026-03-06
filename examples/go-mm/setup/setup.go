package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"time"

	wasmtypes "github.com/CosmWasm/wasmd/x/wasm/types"
	rpchttp "github.com/cometbft/cometbft/rpc/client/http"
	"github.com/joho/godotenv"

	sdk "github.com/cosmos/cosmos-sdk/types"
	txtypes "github.com/cosmos/cosmos-sdk/types/tx"
	authztypes "github.com/cosmos/cosmos-sdk/x/authz"

	chainclient "github.com/InjectiveLabs/sdk-go/client/chain"
	"github.com/InjectiveLabs/sdk-go/client/common"
	cdctypes "github.com/cosmos/cosmos-sdk/codec/types"
)

func mustEnv(key string) string {
	v := os.Getenv(key)
	if v == "" {
		panic(fmt.Sprintf("%s not set", key))
	}
	return v
}

func main() {
	_ = godotenv.Load()

	fmt.Printf("🚀 RFQ Market Maker Setup\n\n")

	/* ---------------------------------------------------------------------- */
	/* ENV & CONFIG                                                            */
	/* ---------------------------------------------------------------------- */
	adminPK := mustEnv("ADMIN_PRIVATE_KEY")
	mmPK := mustEnv("MM_PRIVATE_KEY")
	contractAddr := mustEnv("CONTRACT_ADDRESS")
	chainID := mustEnv("CHAIN_ID")
	// optional
	var settlementContract *string = nil
	network := common.LoadNetwork("local", "lb")
	network.ChainId = chainID

	/* ---------------------------------------------------------------------- */
	/* Tendermint client                                                       */
	/* ---------------------------------------------------------------------- */

	tmClient, err := rpchttp.New(network.TmEndpoint)
	if err != nil {
		panic(err)
	}

	/* ---------------------------------------------------------------------- */
	/* ADMIN keyring + client                                                  */
	/* ---------------------------------------------------------------------- */

	adminAddr, adminKeyring, err := chainclient.InitCosmosKeyring(
		os.Getenv("HOME")+"/.injectived",
		"injectived",
		"file",
		"admin",
		"12345678",
		adminPK,
		false,
	)
	if err != nil {
		panic(err)
	}

	adminCtx, err := chainclient.NewClientContext(
		chainID,
		adminAddr.String(),
		adminKeyring,
	)
	if err != nil {
		panic(err)
	}

	adminCtx = adminCtx.WithNodeURI(network.TmEndpoint).WithClient(tmClient)
	adminChainClient, err := chainclient.NewChainClientV2(adminCtx, network)
	if err != nil {
		panic(err)
	}

	/* ---------------------------------------------------------------------- */
	/* MM keyring + client                                                     */
	/* ---------------------------------------------------------------------- */
	mmAddr, mmKeyring, err := chainclient.InitCosmosKeyring(
		os.Getenv("HOME")+"/.injectived",
		"injectived",
		"file",
		"mm",
		"12345678",
		mmPK,
		false,
	)
	if err != nil {
		panic(err)
	}

	mmCtx, err := chainclient.NewClientContext(
		chainID,
		mmAddr.String(),
		mmKeyring,
	)
	if err != nil {
		panic(err)
	}

	mmCtx = mmCtx.WithNodeURI(network.TmEndpoint).WithClient(tmClient)

	mmChainClient, err := chainclient.NewChainClientV2(mmCtx, network)
	if err != nil {
		panic(err)
	}

	/* ---------------------------------------------------------------------- */
	/* Context & gas                                                           */
	/* ---------------------------------------------------------------------- */

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	// MM gas
	gasPrice := mmChainClient.CurrentChainGasPrice(ctx)
	gasPrice = int64(float64(gasPrice) * 1.1)
	mmChainClient.SetGasPrice(gasPrice)

	/* ---------------------------------------------------------------------- */
	/* STEP 1 — MM grants authz                                                */
	/* ---------------------------------------------------------------------- */

	fmt.Println("Step 1: MM grants permissions to RFQ contract")
	fmt.Println("mm address:", mmChainClient.FromAddress().String())

	grantedMsgs := []string{
		"/injective.exchange.v2.MsgPrivilegedExecuteContract",
		"/cosmos.bank.v1beta1.MsgSend",
	}

	var authzMsgs []sdk.Msg

	for _, msgType := range grantedMsgs {
		genericAuthz, err := (&authztypes.GenericAuthorization{Msg: msgType}).Marshal()
		if err != nil {
			panic(err)
		}

		authzMsgs = append(authzMsgs, &authztypes.MsgGrant{
			Granter: mmAddr.String(),
			Grantee: contractAddr,
			Grant: authztypes.Grant{
				Authorization: &cdctypes.Any{
					TypeUrl: "/cosmos.authz.v1beta1.GenericAuthorization",
					Value:   genericAuthz,
				},
			},
		})
	}

	_, res, err := mmChainClient.BroadcastMsg(
		ctx,
		txtypes.BroadcastMode_BROADCAST_MODE_SYNC,
		authzMsgs...,
	)
	if err != nil {
		panic(err)
	}

	fmt.Println("✅ Permissions granted")
	fmt.Println("TxHash:", res.TxResponse.TxHash)

	/* ---------------------------------------------------------------------- */
	/* STEP 2 — RFQ Admin registers maker                                     */
	/* ---------------------------------------------------------------------- */

	fmt.Println("Step 2: Admin registers Market Maker")

	execMsg := map[string]any{
		"register_maker": map[string]any{
			"maker":               mmAddr.String(),
			"settlement_contract": settlementContract, // for custom behavior, to be supported in v2
		},
	}

	msgBz, err := json.Marshal(execMsg)
	if err != nil {
		panic(err)
	}

	registerMsg := &wasmtypes.MsgExecuteContract{
		Sender:   adminAddr.String(),
		Contract: contractAddr,
		Msg:      msgBz,
		Funds:    sdk.NewCoins(),
	}

	// admin gas
	gasPrice = adminChainClient.CurrentChainGasPrice(ctx)
	gasPrice = int64(float64(gasPrice) * 1.1)
	adminChainClient.SetGasPrice(gasPrice)

	_, res, err = adminChainClient.BroadcastMsg(
		ctx,
		txtypes.BroadcastMode_BROADCAST_MODE_SYNC,
		registerMsg,
	)
	if err != nil {
		panic(err)
	}

	fmt.Println("✅ Maker registered")
	fmt.Println("TxHash:", res.TxResponse.TxHash)

	fmt.Println("\n🎉 RFQ MM setup completed successfully")
}
