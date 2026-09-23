#!/usr/bin/env python3
import json, os, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import coinswitch_v3 as bot

HOST="127.0.0.1"
PORT=int(os.environ.get("COINSWITCH_BRIDGE_PORT","8787"))
LOG_FILE=getattr(bot,"BACKGROUND_LOG_FILE",".coinswitch_v2_background.log")

def snapshot():
    with bot.state_lock:
        states={k:dict(v) for k,v in bot.symbol_states.items()}
        return {
          "ok":True,
          "bot":{
            "ws_connected":bot.ws_connected,
            "paper_mode":bot.PAPER_MODE,
            "live_mode":bot.LIVE_MODE,
            "orders_enabled":bot.ORDERS_ENABLED,
            "low_capital_mode":bot.LOW_CAPITAL_MODE,
            "trailing_enabled":bot.TRAILING_SL_ENABLED,
            "recovery_lock":bot.RECOVERY_LOCK,
            "watchlist_count":len(bot.watchlist)},
          "account":{
            "balance":bot.account_balance,
            "available_balance":bot.account_available_balance,
            "paper_capital":bot.paper_capital},
          "positions":[position(x,s) for x,s in states.items() if s.get("in_position")],
          "config":{"leverage":bot.TRADE_LEVERAGE,"max_sl_risk_pct":bot.MAX_SL_RISK_PCT,
                    "emergency_sl_pct":bot.EMERGENCY_SL_PCT}}

def position(symbol,s):
    return {"symbol":symbol,"side":s.get("position_side"),
      "entry":s.get("position_entry") or s.get("actual_entry"),
      "price":s.get("live_price") or s.get("mark_price"),
      "qty":s.get("position_qty") or s.get("actual_qty"),
      "margin":s.get("position_margin"),"leverage":s.get("actual_leverage") or bot.TRADE_LEVERAGE,
      "sl":s.get("active_sl") or s.get("position_sl"),"sl_status":s.get("sl_status"),
      "protection":s.get("protection_type"),"recovery_status":s.get("recovery_status"),
      "pnl":s.get("exchange_unrealised_pnl"),"trailing_active":s.get("trailing_active"),
      "trailing_armed":s.get("trailing_armed")}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self,code,obj):
        b=json.dumps(obj,default=str).encode()
        self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get("Content-Length","0"))
        return json.loads(self.rfile.read(n) or b"{}")
    def do_GET(self):
        try:
            if self.path=="/api/status": return self.reply(200,snapshot())
            if self.path=="/api/positions": return self.reply(200,{"ok":True,"positions":snapshot()["positions"]})
            if self.path=="/api/logs":
                try:
                    with open(LOG_FILE,encoding="utf-8",errors="replace") as f: lines=f.readlines()[-250:]
                except FileNotFoundError: lines=[]
                return self.reply(200,{"ok":True,"lines":lines})
            return self.reply(404,{"ok":False,"error":"not_found"})
        except Exception as e:
            traceback.print_exc(); return self.reply(500,{"ok":False,"error":str(e)})
    def do_POST(self):
        try:
            p=self.path; d=self.body()
            if p=="/api/control/master":
                bot.ORDERS_ENABLED=bool(d.get("enabled")); return self.reply(200,snapshot())
            if p=="/api/control/mode":
                mode=str(d.get("mode","OFF")).upper()
                if mode=="PAPER": bot.LIVE_MODE=False; bot.PAPER_MODE=True
                elif mode=="LIVE":
                    bot.PAPER_MODE=False; bot.LIVE_MODE=True
                    bot.refresh_account_balance(); bot.recover_live_positions(allow_orders_off=True)
                    bot.start_live_position_sync_thread()
                elif mode=="OFF": bot.PAPER_MODE=False; bot.LIVE_MODE=False
                else: return self.reply(400,{"ok":False,"error":"mode must be PAPER, LIVE or OFF"})
                return self.reply(200,snapshot())
            if p=="/api/control/low-capital":
                bot.LOW_CAPITAL_MODE=bool(d.get("enabled")); return self.reply(200,snapshot())
            if p=="/api/control/trailing":
                bot.TRAILING_SL_ENABLED=bool(d.get("enabled")); return self.reply(200,snapshot())
            if p=="/api/recovery":
                if not bot.LIVE_MODE: return self.reply(400,{"ok":False,"error":"LIVE_MODE must be ON"})
                r=bot.recover_live_positions(allow_orders_off=True); return self.reply(200,{"ok":True,"recovered":bool(r),**snapshot()})
            if p=="/api/emergency-exit-all":
                if d.get("confirm") is not True: return self.reply(400,{"ok":False,"error":"confirmation_required"})
                with bot.state_lock:
                    items=[(x,s.get("live_price") or s.get("mark_price")) for x,s in bot.symbol_states.items() if s.get("in_position") and not s.get("position_closing")]
                results=[]
                for sym,price in items:
                    try:
                        if price is None: raise RuntimeError("no_price")
                        bot.close_position(sym,price,"ANDROID EMERGENCY EXIT ALL"); results.append({"symbol":sym,"ok":True})
                    except Exception as e: results.append({"symbol":sym,"ok":False,"error":str(e)})
                return self.reply(200,{"ok":True,"results":results,"state":snapshot()})
            return self.reply(404,{"ok":False,"error":"not_found"})
        except Exception as e:
            traceback.print_exc(); return self.reply(500,{"ok":False,"error":str(e)})

if __name__=="__main__":
    print(f"Bridge: http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
