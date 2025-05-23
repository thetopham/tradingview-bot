def log_trade_results_to_supabase(acct_id, cid, entry_time, ai_decision_id, meta=None):
    import json
    import time
    from datetime import timedelta
    import logging

    logging.info(f"[log_trade_results_to_supabase] CALLED: acct_id={acct_id}, cid={cid}, entry_time={entry_time}, ai_decision_id={ai_decision_id}, meta={meta}")

    meta = meta or {}

    # --- Normalize entry_time to be timezone-aware (Chicago) ---
    try:
        if not isinstance(entry_time, datetime):
            # If float (timestamp), convert to datetime
            entry_time = datetime.fromtimestamp(entry_time, CT)
        elif entry_time.tzinfo is None:
            entry_time = CT.localize(entry_time)
        else:
            entry_time = entry_time.astimezone(CT)
    except Exception as e:
        logging.error(f"[log_trade_results_to_supabase] entry_time conversion error: {entry_time} ({type(entry_time)}): {e}")
        entry_time = datetime.now(CT)

    # --- Get a timezone-aware exit_time ---
    exit_time = datetime.now(CT)

    # --- Search trades using a window to avoid missing the fill ---
    start_time = entry_time - timedelta(minutes=2)
    try:
        logging.info(f"[log_trade_results_to_supabase] Querying trades: acct_id={acct_id}, cid={cid}, start_time={start_time.isoformat()}")
        resp = post("/api/Trade/search", {
            "accountId": acct_id,
            "startTimestamp": start_time.isoformat()
        })
        trades = resp.get("trades", [])
        logging.info(f"[log_trade_results_to_supabase] All trades returned: {json.dumps(trades, indent=2)}")

        relevant_trades = [
            t for t in trades
            if t.get("contractId") == cid and not t.get("voided", False) and t.get("size", 0) > 0
        ]

        logging.info(f"[log_trade_results_to_supabase] Relevant trades: {json.dumps(relevant_trades, indent=2)}")

        # --- If no relevant trades, log to missing file and skip ---
        if not relevant_trades:
            logging.warning("[log_trade_results_to_supabase] No relevant trades found, skipping Supabase log.")
            try:
                with open("/tmp/trade_results_missing.jsonl", "a") as f:
                    f.write(json.dumps({
                        "acct_id": acct_id,
                        "cid": cid,
                        "entry_time": entry_time.isoformat(),
                        "ai_decision_id": ai_decision_id,
                        "meta": meta,
                        "all_trades": trades
                    }) + "\n")
            except Exception as e2:
                logging.error(f"[log_trade_results_to_supabase] Failed to write missing-trade log: {e2}")
            return

        total_pnl = sum(float(t.get("profitAndLoss") or 0.0) for t in relevant_trades)
        trade_ids = [t.get("id") for t in relevant_trades]
        duration_sec = int((exit_time - entry_time).total_seconds())

        # LOG the raw ai_decision_id and type!
        logging.info(f"[log_trade_results_to_supabase] About to construct payload with ai_decision_id={ai_decision_id} (type={type(ai_decision_id)})")

        # Patch: Accept string or int for ai_decision_id
        ai_decision_id_out = str(ai_decision_id) if ai_decision_id is not None else None

        payload = {
            "strategy":      str(meta.get("strategy") or ""),
            "signal":        str(meta.get("signal") or ""),
            "symbol":        str(meta.get("symbol") or ""),
            "account":       str(meta.get("account") or ""),
            "size":          int(meta.get("size") or 0),
            "ai_decision_id": ai_decision_id_out,
            "entry_time":    entry_time.isoformat() if hasattr(entry_time, "isoformat") else str(entry_time),
            "exit_time":     exit_time.isoformat() if hasattr(exit_time, "isoformat") else str(exit_time),
            "duration_sec":  str(duration_sec) if duration_sec is not None else "0",
            "alert":         str(meta.get("alert") or ""),
            "total_pnl":     float(total_pnl) if total_pnl is not None else 0.0,
            "raw_trades":    relevant_trades if relevant_trades else [],
            "order_id":      str(meta.get("order_id") or ""),
            "comment":       str(meta.get("comment") or ""),
            "trade_ids":     trade_ids if trade_ids else [],
        }

        logging.info(f"[log_trade_results_to_supabase] Final payload to upload: {json.dumps(payload, indent=2)}")

        url = f"{SUPABASE_URL}/rest/v1/trade_results"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
        try:
            logging.info(f"[log_trade_results_to_supabase] Uploading to Supabase: {url}")
            r = session.post(url, json=payload, headers=headers, timeout=(3.05, 10))
            logging.info(f"[log_trade_results_to_supabase] Supabase response status: {r.status_code}, text: {r.text}")
            r.raise_for_status()
        except Exception as e:
            logging.error(f"[log_trade_results_to_supabase] Supabase upload failed: {e}")
            logging.error(f"[log_trade_results_to_supabase] Payload that failed: {json.dumps(payload)[:1000]}")
            try:
                with open("/tmp/trade_results_fallback.jsonl", "a") as f:
                    f.write(json.dumps(payload) + "\n")
                logging.info("[log_trade_results_to_supabase] Trade result written to local fallback log.")
            except Exception as e2:
                logging.error(f"[log_trade_results_to_supabase] Failed to write trade result to local log: {e2}")

    except Exception as e:
        logging.error(f"[log_trade_results_to_supabase] Outer error: {e}")
