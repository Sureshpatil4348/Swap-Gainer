# Drawdown Protection - Feature Documentation

## ✅ Status: **CORRECTED AND VERIFIED**

The drawdown protection feature has been **fixed and updated** to work as intended.

---

## 📋 How It Works

### Configuration
```json
{
  "risk": {
    "drawdown_enabled": true,
    "drawdown_stop": 5.0
  }
}
```

- **`drawdown_enabled`**: Set to `true` to activate drawdown protection
- **`drawdown_stop`**: Percentage threshold (e.g., `5.0` = 5%)

---

## 🔍 What Happens

### The System Checks:
1. **Each account is monitored individually** (Account 1 AND Account 2)
2. **Every automation cycle** (~1 second), the system fetches:
   - Current **Balance** (your account's total funds)
   - Current **Equity** (Balance + unrealized P/L from open trades)

### Drawdown Calculation:
```
Drawdown % = (Balance - Equity) / Balance × 100
```

### Trigger Condition:
**If ANY account's drawdown >= threshold → Close ALL trades in BOTH accounts**

---

## 💡 Real-World Examples

### Example 1: ✅ Safe - No Breach
```
Configuration: drawdown_stop = 5.0%

Account 1:
  Balance: $10,000
  Equity:   $9,600
  Drawdown: (10000 - 9600) / 10000 × 100 = 4.0%
  Status: ✅ OK (below 5%)

Account 2:
  Balance: $5,000
  Equity:  $4,800
  Drawdown: (5000 - 4800) / 5000 × 100 = 4.0%
  Status: ✅ OK (below 5%)

Result: Trades continue normally
```

---

### Example 2: ⚠️ Account 1 Breaches
```
Configuration: drawdown_stop = 5.0%

Account 1:
  Balance: $10,000
  Equity:   $9,400  ← Losing trades
  Drawdown: (10000 - 9400) / 10000 × 100 = 6.0%
  Status: ❌ BREACHED (6% > 5%)

Account 2:
  Balance: $5,000
  Equity:  $5,100  ← Actually in profit!
  Drawdown: (5000 - 5100) / 5000 × 100 = -2.0%
  Status: ✅ OK (in profit)

Result: ⚠️ DRAWDOWN TRIGGERED
Action: Closes ALL trades in BOTH accounts immediately
Reason: Account 1 exceeded the 5% limit
```

---

### Example 3: ⚠️ Account 2 Breaches
```
Configuration: drawdown_stop = 5.0%

Account 1:
  Balance: $10,000
  Equity:  $10,200  ← In profit
  Drawdown: -2.0%
  Status: ✅ OK

Account 2:
  Balance: $5,000
  Equity:  $4,700  ← Losing trades
  Drawdown: (5000 - 4700) / 5000 × 100 = 6.0%
  Status: ❌ BREACHED (6% > 5%)

Result: ⚠️ DRAWDOWN TRIGGERED
Action: Closes ALL trades in BOTH accounts
```

---

## 🎯 Key Behaviors

### ✅ What It DOES:
- ✅ Monitors **each account separately**
- ✅ Triggers if **ANY single account** exceeds the limit
- ✅ Closes **ALL trades in BOTH accounts** when triggered
- ✅ Works even if one account is profitable
- ✅ Protects against asymmetric losses

### ❌ What It DOES NOT:
- ❌ Does NOT require both accounts to breach
- ❌ Does NOT calculate combined/total drawdown across accounts
- ❌ Does NOT wait for trades to close naturally
- ❌ Does NOT prevent new trades after trigger (until you reset)

---

## 🔧 Technical Details

### Location in Code
**File**: `automation.py`  
**Function**: `drawdown_breached()`  
**Lines**: 468-515

### Integration
**File**: `main.py`  
**Function**: `evaluate_automation()`  
**Lines**: 1643-1648

```python
if connected:
    accounts = self._fetch_accounts()  # Gets both account infos
    if accounts and drawdown_breached(config.risk, accounts):
        if trades:
            self._set_automation_status("Drawdown stop triggered. Closing all trades.", ok=False)
        self._close_all_pairs_threadsafe(reason="auto:drawdown")
```

### Execution Flow:
1. Every ~1 second, automation loop runs
2. Fetches account info from both MT5 terminals
3. Checks drawdown for Account 1
4. Checks drawdown for Account 2
5. If either breaches → closes all paired trades immediately
6. Trades are closed in parallel (both accounts simultaneously)

---

## 📊 Comparison: Old vs New Implementation

### ❌ OLD (INCORRECT):
```python
# Combined both accounts into single calculation
total_balance = account1.balance + account2.balance
total_equity = account1.equity + account2.equity
drawdown_pct = ((total_equity - total_balance) / total_balance) * 100
```

**Problem**: Only triggered if **combined** loss exceeded threshold. Could miss scenarios where one account had catastrophic loss while other was profitable.

### ✅ NEW (CORRECT):
```python
# Check EACH account individually
for account in accounts:
    drawdown_pct = ((balance - equity) / balance) * 100
    if drawdown_pct >= threshold:
        return True  # Trigger immediately
```

**Benefit**: Protects each account independently. More conservative and safer.

---

## 🧪 Verified Test Cases

All 7 test cases pass:
1. ✅ Neither account breached → No action
2. ✅ Account 1 breached → Triggers
3. ✅ Account 2 breached → Triggers
4. ✅ Both accounts breached → Triggers
5. ✅ Disabled protection → Never triggers
6. ✅ Exact boundary (5.0%) → Triggers
7. ✅ Just under boundary (4.99%) → No action

Run tests with:
```bash
python3 test_drawdown_only.py
```

---

## ⚙️ Configuration Recommendations

### Conservative (Recommended for live trading):
```json
{
  "risk": {
    "drawdown_enabled": true,
    "drawdown_stop": 3.0  // 3% max loss per account
  }
}
```

### Moderate:
```json
{
  "risk": {
    "drawdown_enabled": true,
    "drawdown_stop": 5.0  // 5% max loss per account
  }
}
```

### Aggressive (Not recommended):
```json
{
  "risk": {
    "drawdown_enabled": true,
    "drawdown_stop": 10.0  // 10% max loss per account
  }
}
```

---

## 🚨 Important Notes

1. **Balance vs Equity**:
   - **Balance**: Your account's total funds (doesn't change until trades close)
   - **Equity**: Balance + unrealized P/L from open positions (real-time)
   - Drawdown measures how much equity has dropped below balance

2. **Per-Account Protection**:
   - If you have $10K in Account 1 and $5K in Account 2
   - With 5% limit, Account 1 can lose max $500 and Account 2 max $250
   - The limits are NOT combined (15K × 5% = $750 total)

3. **Immediate Closure**:
   - When triggered, trades close at current market price
   - No waiting for spread conditions or profit targets
   - May incur slippage during volatile markets

4. **Trade History**:
   - Closed trades are marked with reason: `"auto:drawdown"`
   - Visible in trade history and CSV export

5. **After Trigger**:
   - System continues running
   - Can still manually place trades or reload config
   - Automation will resume on next schedule trigger
   - Consider investigating what caused the drawdown before continuing

---

## 📝 Change Log

**Date**: 2025-10-01  
**Status**: Fixed and Verified

**Changes Made**:
1. Changed from combined-account to per-account calculation
2. Fixed formula: Now correctly measures (Balance - Equity) / Balance
3. Added comprehensive test coverage (7 test cases)
4. Updated documentation with examples
5. Verified integration with automation loop

**Previous Behavior**: Only triggered when combined loss exceeded threshold  
**New Behavior**: Triggers when ANY individual account exceeds threshold  
**Safety Level**: Increased ✅

---

## 🔗 Related Files

- `automation.py` - Core drawdown logic
- `automation_config.json` - Configuration settings
- `test_drawdown_only.py` - Standalone test suite
- `tests/test_automation.py` - Full test suite
- `main.py` - Integration with automation loop

---

**Last Updated**: October 1, 2025  
**Tested**: ✅ All tests passing  
**Production Ready**: ✅ Yes
