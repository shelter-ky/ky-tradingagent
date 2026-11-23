"""Order and position accounting for the synthetic terminal replay."""
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MAIN = ("持有", "盘中即卖即买", "各半执行", "早盘90%线", "早盘加尾盘减半")
FEE = 5.0
EARLY, DELAY, LEVEL = "接近中位数先减半", "越线后分两天卖", "绝对VXN分档"


def decide(rule, current, streak, day, warning_ratio):
    ratio = day["vxn"] / day["vxn_reference"]
    if ratio > 1:
        if not day["gate"]:
            return current, 0
        streak += 1
        if rule == DELAY and streak == 1:
            return min(current, .5), streak
        return 0., streak
    target = 1.
    if rule == EARLY and ratio >= warning_ratio:
        target = .5
    if rule == LEVEL:
        vxn = day["vxn"]
        target = 0. if vxn >= 30 else .25 if vxn >= 25 else .5 if vxn >= 20 else 1.
    if target < current and not day["gate"]:
        return current, 0
    return target, 0


@dataclass(frozen=True)
class Rule:
    family: int
    name: str
    param: float=0
    kind: str=""


@dataclass
class Sleeve:
    cash: float
    qty: int=0
    basis: float=0.
    bought_day: str=""
    bought_slot: int=-1
    sold_day: str=""


def replay(days, symbol, rule, delay, actions, *, record_targets=False):
    weights={"主":1.} if rule.family!=2 else {"早":rule.param,"突":1-rule.param}
    sleeves={k:Sleeve(7000*w) for k,w in weights.items() if w>0}
    due,orders,marks={},[],[]
    effective,am,streak=1.,1.,0
    targets=dict.fromkeys(sleeves,0.) if record_targets else None
    attempts=0
    for day_no,(date,d) in enumerate(days.items()):
        grid,opens,close=d["prices" if symbol=="TQQQ" else "qqq_prices"]
        closes=d["closes" if symbol=="TQQQ" else "qcloses"]
        split=d["split"] if symbol=="TQQQ" else 1.
        for book in sleeves.values():
            book.qty=int(book.qty*split)
        for pay in [k for k in due if k<date]:
            for k,amt in due.pop(pay).items(): sleeves[k].cash+=amt
        if date in actions[symbol]:
            pay,amount=actions[symbol][date]
            amount*=d["scale"] if symbol=="TQQQ" else 1.
            due[pay]={k:book.qty*amount for k,book in sleeves.items()}
            for k,amt in due[pay].items(): sleeves[k].basis-=amt
        planned={}
        def add(slot,key,target):
            if key in sleeves:
                planned.setdefault(slot,{})[key]=target
        if day_no==0:
            for k in sleeves: add(0,k,1.)
        if rule.family==1:
            sells=d[{"wait1":"waited_sell","wait5":"five_min_sell"}.get(rule.kind,"old_sell")]
            if d["gate"]:
                for i in sells: add(i,"主",0.)
            for i in d["old_buy"]: add(i,"主",1.)
        elif rule.family==2:
            stress=d["vxn"]>d["vxn_reference"]
            if not stress or d["gate"]:
                target=float(not stress)
                if d["prefix"]>=5: add(5,"早",target)
                for i in d["sell" if stress else "buy"]: add(i,"突",target)
        elif rule.family>=3:
            if d["prefix"]>=5 and np.isfinite(opens[5]):
                am,streak=decide(rule.kind if rule.family==3 and rule.kind else EARLY,am,streak,d,
                                        rule.param if rule.family==3 else .9)
                if effective!=am: add(5,"主",am)
            slot=len(grid)-10
            qdiv=actions["QQQ"].get(date,(None,0))[1]
            if (rule.family==4 and d["pm_valid"] and d["qcut"]<d["qvwap"] and
                    (d["qcut"]+qdiv)/d["prev_q_close"]-1<=-rule.param):
                add(slot,"主","reduce")
        for slot, proposals in sorted(planned.items()):
            i=slot+delay
            if record_targets and rule.family<3:
                targets.update(proposals)
            pending={}
            for k,proposal in proposals.items():
                b=sleeves[k]
                if rule.family in (1,2) and ((proposal==1 and b.qty) or (proposal==0 and not b.qty)):continue
                if rule.kind=="later" and b.sold_day==date and proposal==1:continue
                if rule.kind=="wait5" and proposal==0 and b.bought_day==date and b.bought_slot>slot-6:continue
                if proposal=="reduce" and not b.qty:continue
                pending[k]=proposal
            if not pending:continue
            attempts+=1
            if i>=len(grid) or not np.isfinite(opens[i]):continue
            raw=opens[i]
            quote=opens[0] if slot==0 else closes[slot-1]
            if delay and not np.isfinite(quote):continue
            fee=FEE/len(pending)
            signed=0; pnl=0.; participating=[]
            for k,value in pending.items():
                b=sleeves[k]
                if value=="reduce":
                    target=0 if rule.kind=="all" else effective/2
                    desired=0 if rule.kind=="all" else b.qty//2
                else:
                    target=value
                    ref=quote if delay else raw
                    desired=max(0,math.floor(target*(b.cash+b.qty*ref-fee)/ref))
                if rule.family>=3: effective=target
                change=desired-b.qty
                px=raw*(1+math.copysign(.0005,change)) if delay and change else raw
                if change>0: change=min(change,max(0,math.floor((b.cash-fee)/px)))
                if not change:continue
                if change<0:
                    exit_basis=b.basis*(-change/b.qty)
                    pnl+=(-change)*px-fee-exit_basis
                    b.basis-=exit_basis
                    b.sold_day=date
                else:
                    b.basis+=change*px+fee
                    b.bought_day,b.bought_slot=date,i
                b.qty+=change
                b.cash-=change*px+fee
                assert b.cash>=-1e-7 and b.qty>=0 and (not signed or np.sign(signed)==np.sign(change))
                signed+=change; participating.append(k)
            if signed:
                assert len(participating)==len(pending) or len(pending)==2 and slot==0
                if len(participating)!=len(pending):
                    # Only the simultaneous first-day buy can combine two sleeves.
                    # All registered allocations can fund their initial lot on these dates.
                    raise ValueError("Initial split ticket must be repriced for a single funded sleeve")
                orders.append(dict(date=date,et=grid[i].tz_convert("America/New_York").isoformat(),slot=int(slot),
                                   delta=signed,price=px,fee=FEE,closed_pnl=pnl if signed<0 else np.nan))
        for k,amt in due.pop(date,{}).items():sleeves[k].cash+=amt
        qty=sum(b.qty for b in sleeves.values())
        cash=sum(b.cash for b in sleeves.values())
        owed=sum(sum(pay.values()) for pay in due.values())
        marks.append(dict(date=date,equity=cash+qty*close+owed,cash=cash,qty=qty,close=close,
                          receivable=owed,unrealized=qty*close-sum(b.basis for b in sleeves.values())))
        if record_targets:
            marks[-1]["targets"]=dict(主=effective) if rule.family>=3 else targets.copy()
    fills=pd.DataFrame(orders)
    daily=pd.DataFrame(marks)
    assert abs(fills.closed_pnl.sum()+daily.unrealized.iloc[-1]-(daily.equity.iloc[-1]-7000))<1e-6
    return fills,daily,attempts


def rules():
    return [Rule(0, MAIN[0]), Rule(1, MAIN[1]), Rule(2, MAIN[2], .5),
            Rule(3, MAIN[3], .9), Rule(4, MAIN[4], .01)]
