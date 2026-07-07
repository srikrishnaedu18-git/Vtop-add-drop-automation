import os
import imaplib
import email
import re
import time
from email.header import decode_header
from dotenv import load_dotenv

load_dotenv()

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()

def get_vtop_otp(max_wait_seconds=60, expected_prefix: str = None) -> tuple[str, str]:
    """
    Connects to Gmail, waits for a new VTOP OTP email, and extracts it.
    If expected_prefix is provided, it skips any unread emails that do not match it.
    Returns: (prefix, otp_code) e.g. ("KNZ", "pABYE")
    Returns (None, None) if not found within max_wait_seconds.
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD or GMAIL_ADDRESS == "your.email@gmail.com":
        print("ERROR: GMAIL_ADDRESS and GMAIL_APP_PASSWORD are not set correctly in .env")
        return None, None

    print(f"\n[Gmail] Connecting to {GMAIL_ADDRESS}...")
    
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    except Exception as e:
        print(f"ERROR logging into Gmail: {e}")
        return None, None

    start_time = time.time()
    
    print(f"[Gmail] Waiting for OTP email from VTOP (Expected Prefix: {expected_prefix or 'Any'})...")
    
    while time.time() - start_time < max_wait_seconds:
        mail.select("inbox")
        
        # Search for UNSEEN emails from the VTOP sender
        status, messages = mail.search(None, '(UNSEEN FROM "noreply.sdc@vit.ac.in")')
        
        if status != "OK":
            time.sleep(2)
            continue
            
        message_ids = messages[0].split()
        
        if message_ids:
            # Get the latest unread email
            latest_id = message_ids[-1]
            status, msg_data = mail.fetch(latest_id, "(RFC822)")
            
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    # Extract email body
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()
                            if content_type == "text/plain":
                                try:
                                    body += part.get_payload(decode=True).decode()
                                except:
                                    pass
                    else:
                        try:
                            body = msg.get_payload(decode=True).decode()
                        except:
                            pass
                            
                    # Clean up body to handle random newlines/spaces
                    body_clean = " ".join(body.split())
                    
                    # Regex pattern matching: "is KNZ - pABYE for"
                    match = re.search(r"is\s+([A-Za-z0-9]{3})\s*-\s*([A-Za-z0-9]+)\s+for", body_clean)
                    
                    if match:
                        prefix = match.group(1)
                        otp_code = match.group(2)
                        
                        # Mark as read so we don't process it again
                        mail.store(latest_id, '+FLAGS', '\\Seen')
                        
                        # If we have an expected prefix and this doesn't match, it's an old OTP. Skip it.
                        if expected_prefix and prefix.strip().upper() != expected_prefix.strip().upper():
                            print(f"  [i] Found VTOP email with prefix '{prefix}', but expected '{expected_prefix}'. Marking as read and continuing search...")
                            continue
                        
                        mail.logout()
                        print(f"  [✓] Found OTP! Prefix: {prefix} | Code: {otp_code}")
                        return prefix, otp_code
                    else:
                        print("  [!] Found VTOP email, but couldn't parse the OTP.")
                        print(f"  Body extract: {body_clean[:100]}")
                        
                        # Mark as read anyway so we don't get stuck on it
                        mail.store(latest_id, '+FLAGS', '\\Seen')
                        
        time.sleep(3) # Wait 3 seconds before polling again
        
    print("  [!] Timeout waiting for OTP email.")
    mail.logout()
    return None, None

if __name__ == "__main__":
    # Test script standalone
    print("Testing Gmail IMAP Connection...")
    prefix, code = get_vtop_otp(max_wait_seconds=10)
    if prefix:
        print(f"Success! OTP: {prefix}-{code}")
    else:
        print("No new OTP emails found right now (which is expected unless you just triggered one).")
