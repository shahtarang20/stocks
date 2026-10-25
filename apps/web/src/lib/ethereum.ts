/**
 * Minimal browser-wallet (EIP-1193) helper: connect, switch network, sign a message.
 *
 * No wallet library is bundled on purpose. Everything here is three RPC methods that
 * every injected wallet (MetaMask, Rabby, Coinbase Wallet, ...) supports, so there is
 * far less code to audit than a wallet-connection stack.
 */

interface Eip1193Provider {
  request(args: { method: string; params?: unknown[] | object }): Promise<unknown>;
}

declare global {
  interface Window {
    ethereum?: Eip1193Provider;
  }
}

export function getWalletProvider(): Eip1193Provider | null {
  return typeof window !== "undefined" && window.ethereum ? window.ethereum : null;
}

/** Networks a wallet may not know yet, so "switch" can fall back to "add". */
const KNOWN_CHAINS: Record<number, { chainName: string; rpcUrls: string[]; nativeCurrency: { name: string; symbol: string; decimals: number }; blockExplorerUrls: string[] }> = {
  56: {
    chainName: "BNB Smart Chain",
    rpcUrls: ["https://bsc-dataseed.binance.org"],
    nativeCurrency: { name: "BNB", symbol: "BNB", decimals: 18 },
    blockExplorerUrls: ["https://bscscan.com"],
  },
  97: {
    chainName: "BNB Smart Chain Testnet",
    rpcUrls: ["https://data-seed-prebsc-1-s1.binance.org:8545"],
    nativeCurrency: { name: "Test BNB", symbol: "tBNB", decimals: 18 },
    blockExplorerUrls: ["https://testnet.bscscan.com"],
  },
  11155111: {
    chainName: "Sepolia",
    rpcUrls: ["https://rpc.sepolia.org"],
    nativeCurrency: { name: "Sepolia Ether", symbol: "ETH", decimals: 18 },
    blockExplorerUrls: ["https://sepolia.etherscan.io"],
  },
  84532: {
    chainName: "Base Sepolia",
    rpcUrls: ["https://sepolia.base.org"],
    nativeCurrency: { name: "Sepolia Ether", symbol: "ETH", decimals: 18 },
    blockExplorerUrls: ["https://sepolia.basescan.org"],
  },
};

/** Chains where the native coin has real value. Anything else is treated as a test network. */
const MAINNET_CHAIN_IDS = new Set([1, 56, 137, 8453, 42161, 10]);

export function isMainnetChain(chainId: number): boolean {
  return MAINNET_CHAIN_IDS.has(chainId);
}

export class WalletError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.name = "WalletError";
    this.code = code;
  }
}

function asWalletError(err: unknown): WalletError {
  const e = err as { code?: number; message?: string };
  if (e?.code === 4001) return new WalletError("rejected", "You declined the request in your wallet.");
  if (e?.code === -32002) return new WalletError("pending", "Your wallet already has a request open. Check the wallet window.");
  return new WalletError("wallet_error", e?.message || "The wallet returned an error.");
}

/** Ask the wallet for an account. Returns the checksummed address. */
export async function connectWallet(): Promise<string> {
  const provider = getWalletProvider();
  if (!provider) throw new WalletError("no_wallet", "No browser wallet found. Install MetaMask or another wallet and reload.");
  try {
    const accounts = (await provider.request({ method: "eth_requestAccounts" })) as string[];
    if (!accounts?.length) throw new WalletError("no_account", "The wallet did not share an account.");
    return accounts[0];
  } catch (err) {
    throw err instanceof WalletError ? err : asWalletError(err);
  }
}

/** Switch the wallet to `chainId`, adding the network first if the wallet doesn't have it. */
export async function ensureChain(chainId: number): Promise<void> {
  const provider = getWalletProvider();
  if (!provider) throw new WalletError("no_wallet", "No browser wallet found.");
  const hex = `0x${chainId.toString(16)}`;
  try {
    const current = (await provider.request({ method: "eth_chainId" })) as string;
    if (parseInt(current, 16) === chainId) return;
    await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: hex }] });
  } catch (err) {
    const code = (err as { code?: number })?.code;
    const known = KNOWN_CHAINS[chainId];
    if ((code === 4902 || code === -32603) && known) {
      try {
        await provider.request({ method: "wallet_addEthereumChain", params: [{ chainId: hex, ...known }] });
        return;
      } catch (addErr) {
        throw asWalletError(addErr);
      }
    }
    throw asWalletError(err);
  }
}

function toHex(text: string): string {
  return "0x" + Array.from(new TextEncoder().encode(text)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** EIP-191 `personal_sign` over a UTF-8 message. Returns the 0x signature. */
export async function signMessage(address: string, message: string): Promise<string> {
  const provider = getWalletProvider();
  if (!provider) throw new WalletError("no_wallet", "No browser wallet found.");
  try {
    return (await provider.request({ method: "personal_sign", params: [toHex(message), address] })) as string;
  } catch (err) {
    throw asWalletError(err);
  }
}

export function shortAddress(address: string): string {
  return address.length > 12 ? `${address.slice(0, 6)}…${address.slice(-4)}` : address;
}
