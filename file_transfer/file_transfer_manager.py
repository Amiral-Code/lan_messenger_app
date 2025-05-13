import json
import os
import threading
import time
import logging
from threading import Lock

# Initialize logger
logger = logging.getLogger(__name__)

class FileTransferManager:
    def __init__(self, username, p2p_manager, gui_update_callback=None, gui_file_prompt_callback=None, default_download_path="./downloads"):
        self.username = username
        self.p2p_manager = p2p_manager
        self.gui_update_callback = gui_update_callback # For progress, completion, errors
        self.gui_file_prompt_callback = gui_file_prompt_callback # To ask user to accept/reject file
        self.default_download_path = default_download_path
        self.active_transfers = {} # {(peer_address, file_id): {details}}
        self.file_id_counter = 0
        self.chunk_size = 4096 * 4 # 16KB chunks for potentially better performance
        self.lock = Lock()  # Ensure thread safety

        if not os.path.exists(self.default_download_path):
            try:
                os.makedirs(self.default_download_path)
            except OSError as e:
                logger.error(f"[FileTransferManager] Error creating download dir {self.default_download_path}: {e}")
                # Fallback handled by main app if needed

    def _generate_file_id(self):
        """Thread-safe file ID generation."""
        with self.lock:
            self.file_id_counter += 1
            return f"file_{self.username.replace(' ', '_')}_{int(time.time())}_{self.file_id_counter}"

    def _handle_file_transfer_error(self, error_message):
        """Log and handle file transfer errors."""
        logger.error(error_message)
        if self.gui_update_callback:
            self.gui_update_callback("Error", "File Transfer", error_message)

    def handle_incoming_file_data(self, peer_address, data):
        try:
            message_str = data.decode("utf-8")
            message_data = json.loads(message_str)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._process_raw_file_chunk(peer_address, data)
            return

        msg_type = message_data.get("type")
        file_id = message_data.get("file_id")

        if msg_type == "file_offer":
            filename = message_data.get("filename")
            filesize = message_data.get("filesize")
            sender_username = message_data.get("sender_username")
            if self.gui_file_prompt_callback:
                self.gui_file_prompt_callback(sender_username, peer_address, file_id, filename, filesize)
            else:
                logger.info(f"[FileTransfer] Auto-accepting file {filename} from {sender_username} ({peer_address}). ID: {file_id}")
                self.accept_file_transfer(peer_address, file_id, filename, filesize)
        
        elif msg_type == "file_accept":
            transfer_key = (peer_address, file_id)
            if transfer_key in self.active_transfers and self.active_transfers[transfer_key]["status"] == "offered":
                self.active_transfers[transfer_key]["status"] = "sending"
                self.active_transfers[transfer_key]["start_time"] = time.time()
                filepath = self.active_transfers[transfer_key]["filepath"]
                if self.gui_update_callback:
                    self.gui_update_callback(file_id, "accepted", f"Peer accepted. Starting transfer of {os.path.basename(filepath)}.")
                thread = threading.Thread(target=self._send_file_chunks, args=(peer_address, file_id, filepath), daemon=True)
                thread.start()
            else:
                logger.warning(f"[FileTransfer] Unexpected file_accept for {file_id} from {peer_address}")

        elif msg_type == "file_reject":
            if (peer_address, file_id) in self.active_transfers:
                if self.gui_update_callback:
                    self.gui_update_callback(file_id, "rejected", f"Peer rejected file transfer.")
                del self.active_transfers[(peer_address, file_id)]
        
        elif msg_type == "file_chunk_header":
            chunk_size_val = message_data.get("chunk_size")
            transfer_key = (peer_address, file_id)
            if transfer_key in self.active_transfers and self.active_transfers[transfer_key]["status"] == "receiving":
                self.active_transfers[transfer_key]["expected_chunk_size"] = chunk_size_val
            else:
                logger.warning(f"[FileTransfer] Unexpected file_chunk_header for {file_id}")

        elif msg_type == "file_transfer_complete":
            transfer_key = (peer_address, file_id)
            if transfer_key in self.active_transfers and self.active_transfers[transfer_key]["status"] == "receiving":
                transfer_info = self.active_transfers[transfer_key]
                if transfer_info.get("file_handle"):
                    transfer_info["file_handle"].close()
                if self.gui_update_callback:
                    self.gui_update_callback(file_id, "completed_receive", f"File {transfer_info['filename']} received successfully!")
                del self.active_transfers[transfer_key]

        elif msg_type == "file_transfer_error":
            error_message = message_data.get("error", "Unknown error")
            transfer_key = (peer_address, file_id)
            if transfer_key in self.active_transfers:
                if self.gui_update_callback:
                    self.gui_update_callback(file_id, "error", f"Transfer error: {error_message}")
                if self.active_transfers[transfer_key].get("file_handle"):
                    self.active_transfers[transfer_key]["file_handle"].close()
                del self.active_transfers[transfer_key]

    def _process_raw_file_chunk(self, peer_address, data_chunk):
        active_receive_transfer = None
        transfer_id_to_process = None

        for key, transfer_info in self.active_transfers.items():
            if key[0] == peer_address and transfer_info["status"] == "receiving" and "file_handle" in transfer_info:
                if "expected_chunk_size" in transfer_info and len(data_chunk) == transfer_info["expected_chunk_size"]:
                    active_receive_transfer = transfer_info
                    transfer_id_to_process = transfer_info["file_id"]
                    break
        
        if active_receive_transfer and transfer_id_to_process:
            try:
                active_receive_transfer["file_handle"].write(data_chunk)
                active_receive_transfer["bytes_received"] += len(data_chunk)
                
                elapsed_time = time.time() - active_receive_transfer["start_time"]
                speed_bps = (active_receive_transfer["bytes_received"] / elapsed_time) if elapsed_time > 0 else 0
                speed_kbps = speed_bps / 1024
                
                if self.gui_update_callback:
                    progress = (active_receive_transfer["bytes_received"] / active_receive_transfer["filesize"]) * 100
                    self.gui_update_callback(transfer_id_to_process, "progress_receive", f"{progress:.1f}% at {speed_kbps:.1f} KB/s")
                
                del active_receive_transfer["expected_chunk_size"]

            except Exception as e:
                logger.error(f"[FileTransfer] Error writing file chunk for {transfer_id_to_process}: {e}")
                self._send_transfer_error(peer_address, transfer_id_to_process, f"Error writing file: {e}")
                active_receive_transfer["file_handle"].close()
                if (peer_address, transfer_id_to_process) in self.active_transfers:
                    del self.active_transfers[(peer_address, transfer_id_to_process)]

    def offer_file(self, peer_address, filepath):
        if not os.path.exists(filepath):
            if self.gui_update_callback:
                self.gui_update_callback(None, "error", f"File not found: {os.path.basename(filepath)}")
            return None
        
        filename = os.path.basename(filepath)
        filesize = os.path.getsize(filepath)
        file_id = self._generate_file_id()

        offer_message = {
            "type": "file_offer", "file_id": file_id, "filename": filename,
            "filesize": filesize, "sender_username": self.username
        }
        if self.p2p_manager.send_data(peer_address, json.dumps(offer_message).encode("utf-8")):
            self.active_transfers[(peer_address, file_id)] = {
                "file_id": file_id, "filepath": filepath, "filename": filename, "filesize": filesize,
                "status": "offered", "bytes_sent": 0, "start_time": 0 # start_time updated on accept
            }
            if self.gui_update_callback:
                self.gui_update_callback(file_id, "offered", f"Offered {filename} to {peer_address[0]}.")
            return file_id
        else:
            if self.gui_update_callback:
                self.gui_update_callback(None, "error", f"Failed to offer {filename} to {peer_address[0]}.")
            return None

    def accept_file_transfer(self, peer_address, file_id, filename, filesize):
        save_path = os.path.join(self.default_download_path, filename)
        count = 1
        base, ext = os.path.splitext(save_path)
        while os.path.exists(save_path):
            save_path = f"{base}_{count}{ext}"
            count += 1
        try:
            file_handle = open(save_path, "wb")
        except Exception as e:
            self._send_transfer_error(peer_address, file_id, f"Failed to open file for saving: {e}")
            if self.gui_update_callback: self.gui_update_callback(file_id, "error", f"Cannot save file: {e}")
            return

        self.active_transfers[(peer_address, file_id)] = {
            "file_id": file_id, "filename": os.path.basename(save_path), "filesize": filesize,
            "status": "receiving", "save_path": save_path, "file_handle": file_handle,
            "bytes_received": 0, "start_time": time.time()
        }
        accept_message = {"type": "file_accept", "file_id": file_id}
        if self.p2p_manager.send_data(peer_address, json.dumps(accept_message).encode("utf-8")):
            if self.gui_update_callback:
                self.gui_update_callback(file_id, "accepting", f"Accepting {filename}. Saving to {os.path.basename(save_path)}.")
        else:
            file_handle.close(); os.remove(save_path)
            if (peer_address, file_id) in self.active_transfers: del self.active_transfers[(peer_address, file_id)]
            if self.gui_update_callback: self.gui_update_callback(file_id, "error", f"Failed to notify acceptance of {filename}.")

    def reject_file_transfer(self, peer_address, file_id, reason="User rejected"):
        reject_message = {"type": "file_reject", "file_id": file_id, "reason": reason}
        self.p2p_manager.send_data(peer_address, json.dumps(reject_message).encode("utf-8"))
        if self.gui_update_callback: self.gui_update_callback(file_id, "rejected_local", f"Rejected file transfer.")

    def _send_file_chunks(self, peer_address, file_id, filepath):
        transfer_key = (peer_address, file_id)
        transfer_info = self.active_transfers.get(transfer_key)
        if not transfer_info or transfer_info["status"] != "sending": return
        
        try:
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(self.chunk_size)
                    if not chunk: break
                    
                    chunk_header = {"type": "file_chunk_header", "file_id": file_id, "chunk_size": len(chunk)}
                    if not self.p2p_manager.send_data(peer_address, json.dumps(chunk_header).encode("utf-8")):
                        raise Exception("Failed to send chunk header")
                    # time.sleep(0.01) # Small delay might not be necessary or could be configurable

                    if not self.p2p_manager.send_data(peer_address, chunk):
                        raise Exception("Failed to send chunk data")
                    
                    transfer_info["bytes_sent"] += len(chunk)
                    elapsed_time = time.time() - transfer_info["start_time"]
                    speed_bps = (transfer_info["bytes_sent"] / elapsed_time) if elapsed_time > 0 else 0
                    speed_kbps = speed_bps / 1024

                    if self.gui_update_callback:
                        progress = (transfer_info["bytes_sent"] / transfer_info["filesize"]) * 100
                        self.gui_update_callback(file_id, "progress_send", f"{progress:.1f}% at {speed_kbps:.1f} KB/s")

            completion_message = {"type": "file_transfer_complete", "file_id": file_id}
            self.p2p_manager.send_data(peer_address, json.dumps(completion_message).encode("utf-8"))
            if self.gui_update_callback:
                self.gui_update_callback(file_id, "completed_send", f"File {transfer_info['filename']} sent successfully!")
            if transfer_key in self.active_transfers: del self.active_transfers[transfer_key]

        except Exception as e:
            logger.error(f"[FileTransfer] Error sending file {file_id} to {peer_address}: {e}")
            self._send_transfer_error(peer_address, file_id, str(e))
            if transfer_key in self.active_transfers: del self.active_transfers[transfer_key]
            if self.gui_update_callback:
                self.gui_update_callback(file_id, "error", f"Error sending {transfer_info.get('filename', 'file')}: {e}")

    def _send_transfer_error(self, peer_address, file_id, error_message):
        error_msg_data = {"type": "file_transfer_error", "file_id": file_id, "error": error_message}
        self.p2p_manager.send_data(peer_address, json.dumps(error_msg_data).encode("utf-8"))

# Example Usage (Conceptual - for testing this module standalone)
if __name__ == "__main__":
    class MockP2PManager:
        def __init__(self): self.on_data_received_ft = None; self.host_ip = "127.0.0.1"
        def send_data(self, _, data): print(f"MockP2P (FT): Sending {data[:100].decode(errors='ignore')}..."); return True

    def gui_update(file_id, status, message): print(f"GUI FT Update: ID={file_id}, Status={status}, Msg={message}")
    def gui_prompt(sender, addr, file_id, name, size): 
        print(f"GUI FT Prompt: From {sender} ({addr}), File: {name} ({size}B), ID: {file_id}. Auto-accepting.")
        ft_manager.accept_file_transfer(addr, file_id, name, size)

    mock_p2p_ft = MockP2PManager()
    ft_manager = FileTransferManager("AliceFT", mock_p2p_ft, gui_update, gui_prompt, "./test_downloads_alice")

    if not os.path.exists("./test_downloads_alice"): os.makedirs("./test_downloads_alice")
    test_file_path = "./test_file_to_send_ft.txt"
    if not os.path.exists(test_file_path): 
        with open(test_file_path, "wb") as f: # Write bytes for consistency
            f.write(b"This is a test file for LAN messenger. " * 20000) # Approx 1MB
    
    bob_addr = ("192.168.1.101", 50001)
    print("\n--- Alice offers file to Bob ---")
    offered_id = ft_manager.offer_file(bob_addr, test_file_path)

    if offered_id:
        print("\n--- Simulating Bob accepting (via gui_prompt callback) ---")
        # To simulate Bob receiving and accepting, we directly call handle_incoming_file_data for Alice
        # as if Bob sent an accept message.
        accept_msg_from_bob = {"type": "file_accept", "file_id": offered_id}
        ft_manager.handle_incoming_file_data(bob_addr, json.dumps(accept_msg_from_bob).encode("utf-8"))
        time.sleep(3) # Allow sending thread to run

    print("\n--- Simulating Alice receiving file offer from Charlie ---")
    charlie_addr = ("192.168.1.102", 50001)
    charlie_file_id = "charlie_doc_1"
    charlie_offer = {"type": "file_offer", "file_id": charlie_file_id, "filename": "report.docx", 
                       "filesize": 512000, "sender_username": "CharlieFT"}
    ft_manager.handle_incoming_file_data(charlie_addr, json.dumps(charlie_offer).encode("utf-8"))
    # gui_prompt will auto-accept, then Alice is ready to receive chunks.
    
    if (charlie_addr, charlie_file_id) in ft_manager.active_transfers and \
       ft_manager.active_transfers[(charlie_addr, charlie_file_id)]["status"] == "receiving":
        print("\n--- Simulating Charlie sending chunks to Alice ---")
        for i in range(5): # Send a few chunks
            chunk_data = os.urandom(ft_manager.chunk_size // 2) # Send smaller chunks for test
            chunk_hdr = {"type": "file_chunk_header", "file_id": charlie_file_id, "chunk_size": len(chunk_data)}
            ft_manager.handle_incoming_file_data(charlie_addr, json.dumps(chunk_hdr).encode("utf-8"))
            ft_manager.handle_incoming_file_data(charlie_addr, chunk_data)
            time.sleep(0.1)
        completion_msg = {"type": "file_transfer_complete", "file_id": charlie_file_id}
        ft_manager.handle_incoming_file_data(charlie_addr, json.dumps(completion_msg).encode("utf-8"))

    print("\nFile transfer manager test finished.")

