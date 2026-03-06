# Market Maker SDK Architecture

## Current Implementation (Demo)

The current MM script (`mm-scripts/main.ts`) has **hardcoded pricing**:

```typescript
// Simple ladder pricing (DEMO ONLY)
const prices = [1.3, 1.4, 1.5];

for (const price of prices) {
  await sendQuote(mmWs!, request, price);
}
```

## Production MM SDK Requirements

### 1. **Pricing Engine Interface**

MMs need to plug in their own pricing logic:

```typescript
interface PricingEngine {
  // Get quote for a given RFQ request
  getQuote(request: RfqRequest): Promise<QuoteResponse | null>;
}

interface QuoteResponse {
  price: number;
  size: number;
  confidence: number;  // 0-1, how confident in this price
}
```

### 2. **Market Data Integration**

MMs need real-time market data:

```typescript
interface MarketDataProvider {
  // Get current mark price
  getMarkPrice(marketId: string): Promise<number>;
  
  // Get orderbook depth
  getOrderbook(marketId: string): Promise<Orderbook>;
  
  // Get recent trades
  getRecentTrades(marketId: string): Promise<Trade[]>;
  
  // Subscribe to price updates
  subscribeToPriceUpdates(marketId: string, callback: (price: number) => void): void;
}
```

### 3. **Risk Management**

MMs need position and exposure tracking:

```typescript
interface RiskManager {
  // Check if quote is within risk limits
  canQuote(request: RfqRequest, price: number): Promise<boolean>;
  
  // Get current position
  getPosition(marketId: string): Promise<Position>;
  
  // Get total exposure across all markets
  getTotalExposure(): Promise<number>;
  
  // Update position after trade
  updatePosition(marketId: string, delta: PositionDelta): void;
}
```

### 4. **Complete MM SDK Structure**

```typescript
class MarketMakerSDK {
  constructor(
    private pricingEngine: PricingEngine,
    private marketData: MarketDataProvider,
    private riskManager: RiskManager,
    private config: MMConfig
  ) {}

  // Main entry point
  async start() {
    // Connect to RFQ WebSocket
    const ws = new WebSocket(this.config.wsUrl);
    
    ws.on('message', async (data) => {
      const request = parseRfqRequest(data);
      
      // 1. Check risk limits
      if (!await this.riskManager.canQuote(request)) {
        console.log('Skipping - risk limits exceeded');
        return;
      }
      
      // 2. Get market data
      const markPrice = await this.marketData.getMarkPrice(request.market_id);
      
      // 3. Generate quote using pricing engine
      const quote = await this.pricingEngine.getQuote(request);
      
      if (!quote) {
        console.log('Skipping - no quote available');
        return;
      }
      
      // 4. Sign and send quote
      await this.sendQuote(request, quote);
      
      // 5. Update risk tracking
      await this.riskManager.updatePosition(request.market_id, {
        size: quote.size,
        price: quote.price
      });
    });
  }
}
```

## Example Pricing Engines

### Simple Spread-Based Pricing

```typescript
class SpreadPricingEngine implements PricingEngine {
  constructor(
    private marketData: MarketDataProvider,
    private spreadBps: number = 10  // 0.1% spread
  ) {}

  async getQuote(request: RfqRequest): Promise<QuoteResponse | null> {
    const markPrice = await this.marketData.getMarkPrice(request.market_id);
    
    // Add spread based on direction
    const spread = markPrice * (this.spreadBps / 10000);
    const price = request.taker_direction === 'long' 
      ? markPrice + spread  // Charge premium for buying
      : markPrice - spread; // Offer discount for selling
    
    return {
      price,
      size: Number(request.output_amount),
      confidence: 0.9
    };
  }
}
```

### Orderbook-Based Pricing

```typescript
class OrderbookPricingEngine implements PricingEngine {
  constructor(
    private marketData: MarketDataProvider,
    private depthBps: number = 50  // Look 0.5% into orderbook
  ) {}

  async getQuote(request: RfqRequest): Promise<QuoteResponse | null> {
    const orderbook = await this.marketData.getOrderbook(request.market_id);
    const size = Number(request.output_amount);
    
    // Calculate VWAP for the requested size
    const vwap = this.calculateVWAP(
      orderbook,
      size,
      request.taker_direction === 'long'
    );
    
    if (!vwap) return null;
    
    return {
      price: vwap,
      size,
      confidence: 0.95
    };
  }
  
  private calculateVWAP(
    orderbook: Orderbook,
    size: number,
    isBuy: boolean
  ): number | null {
    const levels = isBuy ? orderbook.asks : orderbook.bids;
    let remainingSize = size;
    let totalCost = 0;
    
    for (const level of levels) {
      const fillSize = Math.min(remainingSize, level.size);
      totalCost += fillSize * level.price;
      remainingSize -= fillSize;
      
      if (remainingSize <= 0) break;
    }
    
    if (remainingSize > 0) return null; // Not enough liquidity
    
    return totalCost / size;
  }
}
```

### ML-Based Pricing (Advanced)

```typescript
class MLPricingEngine implements PricingEngine {
  constructor(
    private model: PricingModel,
    private marketData: MarketDataProvider
  ) {}

  async getQuote(request: RfqRequest): Promise<QuoteResponse | null> {
    // Gather features
    const features = await this.gatherFeatures(request);
    
    // Run ML model
    const prediction = await this.model.predict(features);
    
    return {
      price: prediction.price,
      size: Number(request.output_amount),
      confidence: prediction.confidence
    };
  }
  
  private async gatherFeatures(request: RfqRequest) {
    const [markPrice, orderbook, recentTrades] = await Promise.all([
      this.marketData.getMarkPrice(request.market_id),
      this.marketData.getOrderbook(request.market_id),
      this.marketData.getRecentTrades(request.market_id)
    ]);
    
    return {
      markPrice,
      bidAskSpread: orderbook.asks[0].price - orderbook.bids[0].price,
      orderImbalance: this.calculateImbalance(orderbook),
      recentVolatility: this.calculateVolatility(recentTrades),
      requestSize: Number(request.output_amount),
      direction: request.taker_direction
    };
  }
}
```

## Deployment Patterns

### Pattern 1: Single MM, Multiple Markets

```typescript
const mm = new MarketMakerSDK({
  pricingEngine: new SpreadPricingEngine(marketData, 10),
  marketData: new InjectiveMarketData(),
  riskManager: new BasicRiskManager({
    maxPositionSize: 1000000,
    maxTotalExposure: 5000000
  }),
  config: {
    wsUrl: 'ws://localhost:4464/ws',
    markets: [
      'INJ/USDT',
      'BTC/USDT',
      'ETH/USDT'
    ]
  }
});

await mm.start();
```

### Pattern 2: Multiple MMs, Specialized Strategies

```typescript
// Conservative MM
const conservativeMM = new MarketMakerSDK({
  pricingEngine: new SpreadPricingEngine(marketData, 20), // Wider spread
  riskManager: new ConservativeRiskManager(),
  // ...
});

// Aggressive MM
const aggressiveMM = new MarketMakerSDK({
  pricingEngine: new OrderbookPricingEngine(marketData, 5), // Tighter pricing
  riskManager: new AggressiveRiskManager(),
  // ...
});

await Promise.all([
  conservativeMM.start(),
  aggressiveMM.start()
]);
```

## Next Steps for Production

1. **Build Core SDK Package**
   - `@rfq/mm-sdk` - Core SDK
   - `@rfq/pricing-engines` - Pre-built pricing strategies
   - `@rfq/market-data` - Market data adapters

2. **Integrations**
   - Injective Exchange API
   - External price feeds (Pyth, Chainlink)
   - CEX APIs for hedging

3. **Monitoring & Analytics**
   - Quote fill rates
   - PnL tracking
   - Risk metrics dashboard

4. **Testing Framework**
   - Backtesting engine
   - Simulation environment
   - Performance benchmarks
