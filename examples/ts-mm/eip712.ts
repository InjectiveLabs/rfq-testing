/**
 * RFQ EIP-712 v2 signing — TypeScript reference.
 *
 * Mirrors the indexer's `SignQuote` typed-data layout byte-for-byte. The
 * protocol does NOT use `eth_signTypedData_v4`; it has a fixed
 * `EIP712Domain` and a hand-rolled `SignQuote` typeHash with each field
 * encoded into a 32-byte word. Decimal fields are hashed as
 * `keccak256(utf8(s))`, so the wire price MUST equal the signed price
 * byte-for-byte.
 *
 * Standalone — no rfq_test dependency. Partners can vendor this file.
 */

import { getBytes, hexlify, keccak256, SigningKey, toUtf8Bytes } from "ethers";

export interface SignQuoteV2Input {
  privateKey: string;          // 0x-prefixed or bare hex
  evmChainId: number;          // 1439 testnet, 1776 mainnet
  contractAddress: string;     // RFQ contract bech32, "inj1..."
  marketId: string;
  rfqId: number | bigint;
  taker?: string | null;       // bech32 inj1...; omit/null for blind quotes
  direction: "long" | "short";
  takerMargin: string;         // FPDecimal string, byte-identical to wire
  takerQuantity: string;
  maker: string;               // bech32 inj1...
  makerSubaccountNonce: number;
  makerQuantity: string;
  makerMargin: string;
  price: string;               // tick-quantized BEFORE signing
  expiryMs?: number | bigint;  // exactly one of expiryMs / expiryHeight
  expiryHeight?: number | bigint;
  minFillQuantity?: string;    // sent as "0" in the digest when omitted
}

export interface SignMakerChallengeV2Input {
  privateKey: string;          // 0x-prefixed or bare hex
  evmChainId: number;
  contractAddress: string;     // bech32
  maker: string;               // bech32 inj1...
  nonceHex: string;            // 32-byte hex, with or without 0x
  expiresAt: number | bigint;
}

const DOMAIN_TYPE =
  "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)";

const SIGN_QUOTE_TYPE =
  "SignQuote(string marketId,uint64 rfqId,address taker,uint8 takerDirection," +
  "string takerMargin,string takerQuantity,address maker,uint32 makerSubaccountNonce," +
  "string makerQuantity,string makerMargin,string price,uint8 expiryKind," +
  "uint64 expiryValue,string minFillQuantity,uint8 bindingKind)";

const STREAM_AUTH_CHALLENGE_TYPE =
  "StreamAuthChallenge(uint64 evmChainId,address maker,bytes32 nonce,uint64 expiresAt)";

// --- bech32 → 20-byte EVM address (HRP = "inj") ------------------------------
//
// Self-contained 5-bit→8-bit reconverter so we don't pull in a bech32 dep just
// for the inline reference. Consult BIP-0173 for the spec.

const BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";

export function bech32ToEvm(addr: string): Uint8Array {
  const sep = addr.lastIndexOf("1");
  if (sep < 1 || sep > addr.length - 7) {
    throw new Error(`bad bech32 layout: ${addr}`);
  }
  const hrp = addr.slice(0, sep).toLowerCase();
  if (hrp !== "inj") {
    throw new Error(`expected inj address, got hrp=${hrp}`);
  }
  const dataPart = addr.slice(sep + 1).toLowerCase();
  const data: number[] = [];
  for (const c of dataPart) {
    const v = BECH32_CHARSET.indexOf(c);
    if (v === -1) throw new Error(`bad bech32 char: ${c}`);
    data.push(v);
  }
  // Strip 6-symbol checksum
  const fiveBit = data.slice(0, -6);

  // Re-pack 5-bit groups → 8-bit bytes (no padding)
  let acc = 0;
  let bits = 0;
  const out: number[] = [];
  for (const v of fiveBit) {
    acc = (acc << 5) | v;
    bits += 5;
    while (bits >= 8) {
      bits -= 8;
      out.push((acc >> bits) & 0xff);
    }
  }
  if (bits >= 5 || (acc & ((1 << bits) - 1)) !== 0) {
    throw new Error("bech32: trailing bits");
  }
  if (out.length !== 20) {
    throw new Error(`expected 20-byte address, got ${out.length}`);
  }
  return new Uint8Array(out);
}

// --- 32-byte word encoders --------------------------------------------------

function uintWord(value: bigint, byteWidth: number): Uint8Array {
  if (value < BigInt(0)) throw new Error(`negative uint: ${value}`);
  const out = new Uint8Array(32);
  let v = value;
  for (let i = 0; i < byteWidth; i++) {
    out[31 - i] = Number(v & BigInt(0xff));
    v >>= BigInt(8);
  }
  if (v !== BigInt(0)) throw new Error(`uint overflow for width ${byteWidth}: ${value}`);
  return out;
}

function stringWord(s: string): Uint8Array {
  return getBytes(keccak256(toUtf8Bytes(s)));
}

function addressWord(b20: Uint8Array): Uint8Array {
  if (b20.length !== 20) throw new Error("addressWord requires 20 bytes");
  const out = new Uint8Array(32);
  out.set(b20, 12);
  return out;
}

function zeroAddressWord(): Uint8Array {
  return new Uint8Array(32);
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let pos = 0;
  for (const p of parts) {
    out.set(p, pos);
    pos += p.length;
  }
  return out;
}

function domainSeparator(evmChainId: number, contractBech32: string): Uint8Array {
  return getBytes(
    keccak256(
      concat(
        stringWord(DOMAIN_TYPE),
        stringWord("RFQ"),
        stringWord("1"),
        uintWord(BigInt(evmChainId), 8),
        addressWord(bech32ToEvm(contractBech32)),
      ),
    ),
  );
}

/**
 * Sign an RFQ quote with EIP-712 v2.
 * Returns a 0x-prefixed 65-byte hex string (r ‖ s ‖ v).
 *
 * Pair this with `sign_mode: "v2"` on the wire payload — the indexer
 * rejects empty values with `value of message.sign_mode must be one of
 * "v1", "v2"`.
 */
export function signQuoteV2(input: SignQuoteV2Input): string {
  if ((input.expiryMs == null) === (input.expiryHeight == null)) {
    throw new Error("provide exactly one of expiryMs or expiryHeight");
  }
  const expiryKind: bigint = input.expiryMs != null ? BigInt(0) : BigInt(1);
  const expiryValue: bigint = BigInt((input.expiryMs ?? input.expiryHeight)!);
  const directionByte: bigint = input.direction === "long" ? BigInt(0) : BigInt(1);
  const mfq = input.minFillQuantity ?? "0";

  const msgHash = getBytes(
    keccak256(
      concat(
        stringWord(SIGN_QUOTE_TYPE),
        stringWord(input.marketId),
        uintWord(BigInt(input.rfqId), 8),
        input.taker ? addressWord(bech32ToEvm(input.taker)) : zeroAddressWord(),
        uintWord(directionByte, 1),
        stringWord(input.takerMargin),
        stringWord(input.takerQuantity),
        addressWord(bech32ToEvm(input.maker)),
        uintWord(BigInt(input.makerSubaccountNonce), 4),
        stringWord(input.makerQuantity),
        stringWord(input.makerMargin),
        stringWord(input.price),
        uintWord(expiryKind, 1),
        uintWord(expiryValue, 8),
        stringWord(mfq),
        uintWord(input.taker ? BigInt(1) : BigInt(0), 1),
      ),
    ),
  );

  const ds = domainSeparator(input.evmChainId, input.contractAddress);
  const digest = keccak256(concat(getBytes("0x1901"), ds, msgHash));

  const pkHex = "0x" + input.privateKey.replace(/^0x/, "");
  const key = new SigningKey(pkHex);
  const sig = key.sign(digest);
  const yParity = sig.yParity ?? (sig.v >= 27 ? sig.v - 27 : sig.v);
  return hexlify(
    concat(getBytes(sig.r), getBytes(sig.s), new Uint8Array([yParity & 1])),
  );
}

export function signMakerChallengeV2(input: SignMakerChallengeV2Input): string {
  const nonceHex = input.nonceHex.replace(/^0x/, "");
  if (nonceHex.length !== 64) {
    throw new Error(`expected 32-byte nonce hex, got ${nonceHex.length / 2} bytes`);
  }

  const msgHash = getBytes(
    keccak256(
      concat(
        stringWord(STREAM_AUTH_CHALLENGE_TYPE),
        uintWord(BigInt(input.evmChainId), 8),
        addressWord(bech32ToEvm(input.maker)),
        getBytes("0x" + nonceHex),
        uintWord(BigInt(input.expiresAt), 8),
      ),
    ),
  );

  const ds = domainSeparator(input.evmChainId, input.contractAddress);
  const digest = keccak256(concat(getBytes("0x1901"), ds, msgHash));

  const pkHex = "0x" + input.privateKey.replace(/^0x/, "");
  const key = new SigningKey(pkHex);
  const sig = key.sign(digest);
  const yParity = sig.yParity ?? (sig.v >= 27 ? sig.v - 27 : sig.v);
  return hexlify(
    concat(getBytes(sig.r), getBytes(sig.s), new Uint8Array([yParity & 1])),
  );
}
