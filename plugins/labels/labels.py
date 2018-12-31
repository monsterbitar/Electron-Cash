import hashlib
import requests
import threading
import json
import sys
import traceback

import base64

from electroncash.bitcoin import aes_decrypt_with_iv, aes_encrypt_with_iv
from electroncash.plugins import BasePlugin, hook
from electroncash.i18n import _

class LabelsPlugin(BasePlugin):

    def __init__(self, parent, config, name):
        BasePlugin.__init__(self, parent, config, name)
        self.target_host = 'labels.electrum.org'
        self.wallets = {}
        self.threads = []

    def encode(self, wallet, msg):
        password, iv, wallet_id = self.wallets[wallet]
        encrypted = aes_encrypt_with_iv(password, iv, msg.encode('utf8'))
        return base64.b64encode(encrypted).decode()

    def decode(self, wallet, message):
        password, iv, wallet_id = self.wallets[wallet]
        decoded = base64.b64decode(message)
        decrypted = aes_decrypt_with_iv(password, iv, decoded)
        return decrypted.decode('utf8')

    def get_nonce(self, wallet):
        with wallet.lock:
            # nonce is the nonce to be used with the next change
            nonce = wallet.storage.get('wallet_nonce')
            if nonce is None:
                nonce = 1
                self.set_nonce(wallet, nonce)
            return nonce

    def set_nonce(self, wallet, nonce):
        with wallet.lock:
            self.print_error("set", wallet.basename(), "nonce to", nonce)
            wallet.storage.put("wallet_nonce", nonce)

    @hook
    def set_label(self, wallet, item, label):
        if not wallet in self.wallets:
            return
        if not item:
            return
        nonce = self.get_nonce(wallet)
        wallet_id = self.wallets[wallet][2]
        bundle = {"walletId": wallet_id,
                  "walletNonce": nonce,
                  "externalId": self.encode(wallet, item),
                  "encryptedLabel": self.encode(wallet, label)}
        t = threading.Thread(target=self.do_request,
                             args=["POST", "/label", False, bundle, True])
        t.setDaemon(True)
        self.threads.append(t)
        t.start()
        # Caller will write the wallet
        self.set_nonce(wallet, nonce + 1)

    def find_wallet_by_id(self, wallet_id):
        for wallet, tup in self.wallets.copy().items():
            if wallet_id == tup[2]:
                return wallet
        return None

    def do_request(self, method, url = "/labels", is_batch=False, data=None, noexc = False):
        wallet_id = data.get("walletId", None) if data else None
        try:
            #self.print_error("do_request",method,url,is_batch,data,"...")
            url = 'https://' + self.target_host + url
            kwargs = {'headers': {}}
            if method == 'GET' and data:
                kwargs['params'] = data
            elif method == 'POST' and data:
                kwargs['data'] = json.dumps(data)
                kwargs['headers']['Content-Type'] = 'application/json'

            response = requests.request(method, url, **kwargs, timeout=5.0) # will raise requests.exceptions.Timeout on timeout

            if response.status_code == 400:
                if "serverNonce is larger then walletNonce" in response.text:
                    wallet = self.find_wallet_by_id(wallet_id)
                    if wallet: self.on_wallet_not_synched(wallet)
                    return
            if response.status_code != 200:
                raise BaseException(response.status_code, response.text)
            response = response.json()
            if "error" in response:
                raise BaseException(response["error"])
            return response
        except BaseException as e:
            if noexc:
                wallet = self.find_wallet_by_id(wallet_id)
                if wallet: self.on_request_exception(wallet, sys.exc_info())
                return
            raise e
        finally:
            try:
                self.threads.remove(threading.current_thread())
            except ValueError: pass # Not in thread list

    def push_thread(self, wallet):
        try:
            if not wallet in self.wallets: return # still has race conditions here
            #self.print_error("push_thread", wallet.basename(),"...")
            wallet_id = self.wallets[wallet][2]
            bundle = {"labels": [],
                      "walletId": wallet_id,
                      "walletNonce": self.get_nonce(wallet)}
            with wallet.lock:
                labels = wallet.labels.copy()
            for key, value in labels.items():
                try:
                    encoded_key = self.encode(wallet, key)
                    encoded_value = self.encode(wallet, value)
                except:
                    self.print_error('cannot encode', repr(key), repr(value))
                    continue
                bundle["labels"].append({'encryptedLabel': encoded_value,
                                         'externalId': encoded_key})

            self.do_request("POST", "/labels", True, bundle)
        finally:
            try:
                self.threads.remove(threading.current_thread())
            except ValueError: pass # Not in thread list

    def pull_thread(self, wallet, force):
        try:
            if not wallet in self.wallets: return # still has race conditions here
            #self.print_error("pull_thread", wallet.basename(),"...")
            wallet_id = self.wallets[wallet][2]
            nonce = 1 if force else self.get_nonce(wallet) - 1
            self.print_error("asking for labels since nonce", nonce)
            try:
                response = self.do_request("GET", ("/labels/since/%d/for/%s" % (nonce, wallet_id) ))
                if response["labels"] is None:
                    self.print_error('no new labels')
                    return
                result = {}
                for label in response["labels"]:
                    try:
                        key = self.decode(wallet, label["externalId"])
                        value = self.decode(wallet, label["encryptedLabel"])
                    except:
                        continue
                    try:
                        json.dumps(key)
                        json.dumps(value)
                    except:
                        self.print_error('error: no json', key)
                        continue
                    result[key] = value

                with wallet.lock:
                    for key, value in result.items():
                        if force or not wallet.labels.get(key):
                            wallet.labels[key] = value

                    self.print_error("received %d labels" % len(response))
                    # do not write to disk because we're in a daemon thread
                    wallet.storage.put('labels', wallet.labels)
                    self.set_nonce(wallet, response["nonce"] + 1)
                self.on_pulled(wallet)

            except Exception as e:
                #traceback.print_exc(file=sys.stderr)
                self.print_error("could not retrieve labels:",str(e))
                if force: raise e # force download means we were in "settings" mode.. notify gui of failure.
        finally:
            try:
                self.threads.remove(threading.current_thread())
            except ValueError: pass # Not in thread list

    def on_pulled(self, wallet):
        pass

    def on_wallet_not_synched(self, wallet):
        pass

    def on_request_exception(self, wallet, exc_info):
        pass

    def start_wallet(self, wallet):
        if wallet in self.wallets:
            self.print_error("Wallet",wallet.basename(),"already in wallets, aborting early.")
            return
        nonce = self.get_nonce(wallet)
        self.print_error("wallet", wallet.basename(), "nonce is", nonce)
        mpk = wallet.get_fingerprint()
        if not mpk:
            return
        mpk = mpk.encode('ascii')
        password = hashlib.sha1(mpk).hexdigest()[:32].encode('ascii')
        iv = hashlib.sha256(password).digest()[:16]
        wallet_id = hashlib.sha256(mpk).hexdigest()
        self.wallets[wallet] = (password, iv, wallet_id)
        # If there is an auth token we can try to actually start syncing
        t = threading.Thread(target=self.pull_thread, args=(wallet, False))
        t.setDaemon(True)
        self.threads.append(t)
        t.start()
        self.print_error(wallet.basename(),"added.")
        return True

    def stop_wallet(self, wallet):
        w = self.wallets.pop(wallet, None)
        if w:
            self.print_error(wallet.basename(),"removed from wallets.")
        return bool(w)

    def on_close(self):
        ct = 0
        for w in self.wallets.copy():
            ct += int(bool(self.stop_wallet(w)))
        stopped = 0
        thrds = self.threads.copy()
        for t in thrds:
            if t.is_alive():
                t.join() # wait for it to complete
                stopped += 1
        self.print_error("Plugin closed, stopped {} extant wallets, joined {} extant threads.".format(ct, stopped))
        assert 0 == len(self.threads), "Labels Plugin: Threads were left alive on close!"

