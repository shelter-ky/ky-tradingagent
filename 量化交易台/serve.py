"""Serve the local dashboard and the current Agent's read-only minute observations."""
import argparse
import json
import mimetypes
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

PANEL=Path(__file__).resolve().parent
FILES={"/":PANEL/"交易面板.html"}
for name in ("交易面板.html","terminal.css","terminal.js","回放数据.js"):
    for prefix in ("/","/量化交易台/"):
        FILES[prefix+name]=PANEL/name

LOGS=Path(os.environ.get("TRADINGAGENTS_RESULTS_DIR") or Path.home()/".tradingagents"/"logs")
REPORT_KEYS=("version","ticker","generated_at","session","action","status","reason","valid_until")
QUOTE_KEYS=("ticker","price","vwap","ema9","ema20","momentum5_pct","bar_end")
SELL_KEYS=("status","reason","sample_start","sample_end","sessions","anchor","unit_usd","sale_fee_usd","cost_bps")
OUTCOME_KEYS=("horizon","n","days","down_saved","rebound_missed","average_net","profitable")


def latest_signal(symbol, strategy="intraday"):
    if strategy not in ("intraday","opening0935"):
        raise ValueError("Invalid strategy")
    directory=LOGS/symbol/strategy
    # Agent run directory names begin with the UTC start timestamp.
    latest = max(directory.glob("*/latest.json"), key=lambda path: path.parent.name, default=None)
    if latest is None:
        return None
    item=json.loads(latest.read_text(encoding="utf-8-sig"))
    if not isinstance(item,dict) or item.get("version")!="intraday-v1" or item.get("ticker")!=symbol:
        raise ValueError("Invalid signal")
    snapshots=item.get("snapshots")
    if not isinstance(snapshots,list) or not all(isinstance(s,dict) and isinstance(s.get("ticker"),str) for s in snapshots):
        raise ValueError("Invalid snapshots")
    report={key:item.get(key) for key in REPORT_KEYS}
    report["strategy_id"]=item.get("strategy_id","minute-trend-v1")
    if (report["strategy_id"]=="opening0935-v1") != (strategy=="opening0935"):
        raise ValueError("Mismatched strategy")
    if isinstance(item.get("opening"),dict):
        report["opening"]={key:item["opening"].get(key) for key in ("previous","target","vxn","median","vxn_date","pre")}
    if isinstance(item.get("forward_events"),list):
        records=[]
        for event in item["forward_events"][:20]:
            if not isinstance(event,dict):
                continue
            entry={k:event.get(k) for k in ("signal","bar_end","earliest","fill","qty","gap","buy_signal","buy_price","short_horizon")}
            for key in ("after15_result","closing_result"):
                if isinstance(event.get(key),dict):
                    entry[key]={k:event[key].get(k) for k in ("avoided","missed","fees","net")}
            records.append(entry)
        report["forward_events"]=records
    report["forward_note"]=item.get("forward_note")
    report["snapshots"]=[{key:s.get(key) for key in QUOTE_KEYS} for s in snapshots]
    sizing=item.get("sizing")
    if isinstance(sizing,dict):
        report["sizing"]={key:sizing.get(key) for key in ("status","missing","reason","held","available")}
        if isinstance(sizing.get("validated_allocation"),dict):
            report["sizing"]["validated_allocation"]={k:sizing["validated_allocation"].get(k) for k in ("target_quantity","delta_quantity")}
    analysis=item.get("analysis")
    impact=analysis.get("sell_impact") if isinstance(analysis,dict) else None
    if isinstance(impact,dict):
        summary={key:impact.get(key) for key in SELL_KEYS}
        rows = impact.get("rows")
        summary["rows"] = [
            {key: row.get(key) for key in OUTCOME_KEYS}
            for row in rows if isinstance(row, dict)
        ] if isinstance(rows, list) else []
        report["analysis"]={"sell_impact":summary}
    context=item.get("premarket_context")
    pre=context.get("premarket") if isinstance(context,dict) else None
    if isinstance(pre,dict):
        report["premarket_context"]={"premarket":{k:pre.get(k) for k in ("status","direction","reason")}}
    return report


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def do_GET(self):
        if self.headers.get("Host")!=f"127.0.0.1:{self.server.server_port}":
            self.send_error(403)
            return
        path=unquote(urlsplit(self.path).path)
        if path in ("/signal/QQQ","/signal/TQQQ"):
            try:
                strategy=parse_qs(urlsplit(self.path).query).get("strategy",["intraday"])[0]
                payload=json.dumps({"report":latest_signal(path.rsplit("/",1)[1],strategy)},allow_nan=False).encode()
            except (OSError,ValueError,KeyError,TypeError):
                self.send_error(503)
                return
            kind="application/json"
        elif path in FILES and FILES[path].is_file():
            source=FILES[path]
            payload=source.read_bytes()
            kind={".js":"text/javascript",".html":"text/html",".css":"text/css",".csv":"text/csv"}.get(
                source.suffix,mimetypes.guess_type(source.name)[0] or "application/octet-stream")
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type",kind+"; charset=utf-8")
        self.send_header("Content-Length",str(len(payload)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.end_headers()
        self.wfile.write(payload)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--no-browser",action="store_true")
    args=parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1",0),Handler) as server:
        url=f"http://127.0.0.1:{server.server_port}/"
        print(url,flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
