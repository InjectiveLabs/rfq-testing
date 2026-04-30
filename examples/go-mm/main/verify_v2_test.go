package main

import (
	"testing"
)

// Cross-language sanity: same inputs as scripts/test_signing.py and the
// TS test in onboarding.html should produce the same r||s||v hex.
//
// Reference (Python lib output):
//   0xd31c33b8f17fa16b1bcdbc94cdf7596b6d57d0eec51cbcdd7c1cac2cd774c02b36523d0e711b58096e97c650c1e758f9020121226e935acb9fa7bdbcf8cb63d501
func TestSignQuoteV2_MatchesPythonReference(t *testing.T) {
	const expected = "0xd31c33b8f17fa16b1bcdbc94cdf7596b6d57d0eec51cbcdd7c1cac2cd774c02b36523d0e711b58096e97c650c1e758f9020121226e935acb9fa7bdbcf8cb63d501"

	sig, err := signQuoteV2(signQuoteInput{
		PrivateKey:           "1111111111111111111111111111111111111111111111111111111111111111",
		EvmChainID:           1439,
		ContractAddress:      "inj1qw7jk82hjvf79tnjykux6zacuh9gl0z0wl3ruk",
		MarketID:             "0xdc70164d7120529c3cd84278c98df4151210c0447a65a2aab03459cf328de41e",
		RfqID:                42,
		Taker:                "inj1qw7jk82hjvf79tnjykux6zacuh9gl0z0wl3ruk",
		Direction:            "long",
		TakerMargin:          "100",
		TakerQuantity:        "1",
		Maker:                "inj1qw7jk82hjvf79tnjykux6zacuh9gl0z0wl3ruk",
		MakerSubaccountNonce: 0,
		MakerQuantity:        "1",
		MakerMargin:          "100",
		Price:                "4.5",
		ExpiryKind:           0,
		ExpiryValue:          1700000000000,
		MinFillQuantity:      "",
	})
	if err != nil {
		t.Fatalf("signQuoteV2: %v", err)
	}
	if sig != expected {
		t.Fatalf("signature mismatch:\n  got      %s\n  expected %s", sig, expected)
	}
}
