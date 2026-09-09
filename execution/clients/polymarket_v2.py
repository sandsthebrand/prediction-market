"""Polymarket CLOB V2 execution client."""
from __future__ import annotations
import asyncio,logging,os,time
import aiosqlite
from core.secrets import get_secret
from execution.clients.base import BaseExecutionClient,OrderResult
from execution.clients.polymarket_book import BookResolver
from execution.enums import Side
logger=logging.getLogger(__name__)
class PolymarketExecutionClientV2(BaseExecutionClient):
    def __init__(self,db_connection:aiosqlite.Connection,private_key=None,funder=None,chain_id=137):
        super().__init__(db_connection,platform_label="polymarket"); self._book_resolver=BookResolver(db_connection); self.private_key=private_key or get_secret("POLYMARKET_PRIVATE_KEY","") or ""; self.funder=funder or get_secret("POLYMARKET_WALLET_ADDRESS","") or ""; self.chain_id=chain_id; self.host=os.getenv("POLYMARKET_API_BASE","https://clob.polymarket.com"); self.signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE","0")); self._client=None; self._initialized=False; self._translated_orders={}
    def _ensure_client(self):
        if self._initialized:return
        from py_clob_client_v2 import ApiCreds,ClobClient
        if not self.private_key:raise ValueError("POLYMARKET_PRIVATE_KEY is required")
        kwargs={"host":self.host,"chain_id":self.chain_id,"key":self.private_key}
        if self.funder:kwargs.update(funder=self.funder,signature_type=self.signature_type)
        ak=get_secret("POLYMARKET_API_KEY","") or ""; sec=get_secret("POLYMARKET_API_SECRET","") or ""; pp=get_secret("POLYMARKET_API_PASSPHRASE","") or ""
        if ak and sec and pp:kwargs["creds"]=ApiCreds(api_key=ak,api_secret=sec,api_passphrase=pp)
        self._client=ClobClient(**kwargs)
        if "creds" not in kwargs:self._client.set_api_creds(self._client.create_or_derive_api_key())
        self._initialized=True
    async def _call(self,fn,*args,**kwargs):return await asyncio.to_thread(fn,*args,**kwargs)
    async def submit_order(self,leg,signal_id=None,strategy=None):
        start=time.time()
        try:
            resolved=await self._book_resolver.resolve(leg.market_id,leg.side,leg.size,leg.limit_price)
            if resolved is None:raise ValueError("BookResolver rejected order")
            self._ensure_client(); self._translated_orders[str(leg.market_id)]=resolved.translated
            from py_clob_client_v2 import OrderArgs,OrderType,PartialCreateOrderOptions,Side as PolySide
            side=PolySide.BUY if resolved.side is Side.BUY else PolySide.SELL; tick=await self._call(self._client.get_tick_size,resolved.token_id)
            response=await self._call(self._client.create_and_post_order,OrderArgs(token_id=resolved.token_id,price=resolved.limit_price,side=side,size=resolved.size),PartialCreateOrderOptions(tick_size=str(tick)),OrderType.GTC)
            oid=response.get("orderID") or response.get("order_id") or response.get("id")
            if not oid:raise RuntimeError(f"Polymarket V2 returned no order id: {response}")
            await self.write_order(leg,OrderResult(order_id=oid,platform="polymarket",status="pending",submission_latency_ms=int((time.time()-start)*1000)),signal_id=signal_id,strategy=strategy); return await self._poll(oid,leg,start)
        except Exception as exc:
            result=OrderResult(order_id=f"FAILED-{leg.market_id}",platform="polymarket",status="failed",submission_latency_ms=int((time.time()-start)*1000),error_message=str(exc)); await self.write_order(leg,result,signal_id=signal_id,strategy=strategy); logger.exception("Polymarket V2 order failed"); return result
    async def _poll(self,oid,leg,start,max_polls=40):
        for _ in range(max_polls):
            await asyncio.sleep(.25); order=await self._call(self._client.get_order,oid); status=str(order.get("status","")).upper(); matched=float(order.get("size_matched",order.get("sizeMatched",0)) or 0)
            if matched>0 and status in {"LIVE","DELAYED"}: await self.cancel_order(oid); status="CANCELLED"
            if status in {"MATCHED","UNMATCHED","CANCELED","CANCELLED"}:
                if matched>0:
                    price=float(order.get("price",leg.limit_price or 0)); fee=await self._estimate_fee(leg.market_id,price,matched); result=OrderResult(order_id=oid,platform="polymarket",status="filled" if matched>=leg.size else "partially_filled",submission_latency_ms=int((time.time()-start)*1000),fill_latency_ms=int((time.time()-start)*1000),filled_price=price,filled_size=matched,fee_paid=fee); await self.update_order_fill(result); await self.write_fill_event(result); return result
                result=OrderResult(order_id=oid,platform="polymarket",status="failed",submission_latency_ms=int((time.time()-start)*1000),error_message=f"terminal status={status}"); await self.update_order_fill(result); return result
        await self.cancel_order(oid); return OrderResult(order_id=oid,platform="polymarket",status="pending",submission_latency_ms=int((time.time()-start)*1000),error_message="fill poll timeout; cancelled and requires reconciliation")
    async def _estimate_fee(self,condition_id,price,size):
        try:
            info=await self._call(self._client.get_clob_market_info,condition_id); rate=float((info.get("fd") or {}).get("r",0.0)); return round(size*rate*price*(1.0-price),5)
        except Exception:return 0.0
    def economic_fill_price(self,order_id,price):
        return 1.0-float(price) if self._translated_orders.get(str(order_id),False) else float(price)
    async def cancel_order(self,oid):
        try:
            from py_clob_client_v2 import OrderPayload
            await self._call(self._client.cancel_order,OrderPayload(orderID=oid)); return True
        except Exception:logger.exception("Failed to cancel Polymarket V2 order %s",oid); return False
    async def get_order_status(self,oid):
        try:self._ensure_client(); return await self._call(self._client.get_order,oid)
        except Exception:return None
    async def get_balance(self):
        try:
            self._ensure_client(); from py_clob_client_v2 import BalanceAllowanceParams,AssetType; result=await self._call(self._client.get_balance_allowance,BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)); raw=result.get("balance",result.get("balance_dollars",0)); return float(raw)/1e6 if float(raw)>1000 else float(raw)
        except Exception:logger.exception("Polymarket V2 balance lookup failed"); return None
    async def close(self):return None
