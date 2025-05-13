import json
import time
import logging

# Initialize logger
logger = logging.getLogger(__name__)

class ChatManager:
    def __init__(self, username, p2p_manager, gui_update_callback=None, gui_typing_status_callback=None, gui_read_receipt_callback=None):
        self.username = username
        self.p2p_manager = p2p_manager
        self.gui_update_callback = gui_update_callback # To append messages to GUI
        self.gui_typing_status_callback = gui_typing_status_callback # To update typing status in GUI
        self.gui_read_receipt_callback = gui_read_receipt_callback # To update message status with read receipts

        if self.p2p_manager:
            # on_data_received is handled by the main app's router
            pass

    def _handle_incoming_data(self, peer_address, data):
        """
        Handle incoming chat data.

        Args:
            peer_address (tuple): Address of the peer sending the data.
            data (bytes): The data received.
        """
        try:
            message_str = data.decode("utf-8")
            message_data = json.loads(message_str)
            msg_type = message_data.get("type")
            sender_ip = peer_address[0]

            if msg_type == "chat_message":
                sender = message_data.get("sender", "Unknown")
                content = message_data.get("content", "")
                timestamp = message_data.get("timestamp", time.time())
                message_id = message_data.get("message_id") # Expecting a unique message ID
                
                formatted_message = f"{time.strftime('%H:%M:%S', time.localtime(timestamp))} {sender}: {content}"
                if self.gui_update_callback:
                    # Pass message_id so GUI can track it for read receipts
                    self.gui_update_callback(formatted_message, peer_address, message_id=message_id)
                
                # Send read receipt if message_id is present and not from self
                if message_id and sender != self.username:
                    self.send_read_receipt(peer_address, message_id)
            
            elif msg_type == "group_chat_message":
                sender = message_data.get("sender", "Unknown")
                content = message_data.get("content", "")
                timestamp = message_data.get("timestamp", time.time())
                message_id = message_data.get("message_id")

                if sender == self.username and peer_address[0] == self.p2p_manager.host_ip:
                     return
                
                formatted_message = f"{time.strftime('%H:%M:%S', time.localtime(timestamp))} {sender} (Group): {content}"
                if self.gui_update_callback:
                    self.gui_update_callback(formatted_message, None, message_id=message_id) # Group messages might also have IDs
                
                # Group read receipts are complex (who read it?). For now, only for direct messages.
                # If group read receipts were to be implemented, it would need a different mechanism.

            elif msg_type == "chat_typing_status":
                sender_username = message_data.get("sender_username")
                is_typing = message_data.get("is_typing", False)
                if self.gui_typing_status_callback and sender_username != self.username:
                    self.gui_typing_status_callback(sender_username, sender_ip, is_typing)
            
            elif msg_type == "chat_read_receipt":
                reader_username = message_data.get("reader_username")
                original_message_id = message_data.get("message_id")
                if self.gui_read_receipt_callback and original_message_id:
                    self.gui_read_receipt_callback(reader_username, sender_ip, original_message_id)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON from {peer_address}: {e}")
        except UnicodeDecodeError:
            logger.error(f"Received non-UTF8 message from {peer_address}")
        except Exception as e:
            logger.error(f"Unexpected error handling data from {peer_address}: {e}")

    def _generate_message_id(self):
        return f"msg_{self.username.replace(' ', '_')}_{int(time.time()*1000)}_{time.time_ns() % 1000}"

    def send_chat_message(self, recipient_address, message_content):
        if not message_content.strip():
            return False
        message_id = self._generate_message_id()
        message_data = {
            "type": "chat_message",
            "message_id": message_id,
            "sender": self.username,
            "content": message_content,
            "timestamp": time.time()
        }
        try:
            data_bytes = json.dumps(message_data).encode("utf-8")
            if self.p2p_manager.send_data(recipient_address, data_bytes):
                return True, message_id # Return message_id for GUI to track
            return False, None
        except Exception as e:
            logger.error(f"Error sending chat message: {e}")
            return False, None

    def send_group_chat_message(self, message_content):
        if not message_content.strip():
            return False
        message_id = self._generate_message_id()
        message_data = {
            "type": "group_chat_message",
            "message_id": message_id,
            "sender": self.username,
            "content": message_content,
            "timestamp": time.time()
        }
        try:
            data_bytes = json.dumps(message_data).encode("utf-8")
            sent_to_any = False
            if not self.p2p_manager.connections:
                if self.gui_update_callback:
                    self.gui_update_callback("[System] No peers connected to send group message.", None)
                return False, None
                
            for peer_addr in list(self.p2p_manager.connections.keys()):
                if self.p2p_manager.send_data(peer_addr, data_bytes):
                    sent_to_any = True
            
            if sent_to_any:
                formatted_sent_message = f"{time.strftime('%H:%M:%S', time.localtime(message_data['timestamp']))} You (Group): {message_content}"
                if self.gui_update_callback:
                    # Pass message_id for self-sent group messages too, though receipts aren't for them
                    self.gui_update_callback(formatted_sent_message, None, is_self=True, message_id=message_id)
                return True, message_id
            else:
                if self.gui_update_callback:
                    self.gui_update_callback("[System] Failed to send group message.", None)
                return False, None
        except Exception as e:
            logger.error(f"Error sending group chat message: {e}")
            return False, None

    def send_typing_status(self, recipient_address, is_typing):
        message_data = {
            "type": "chat_typing_status",
            "sender_username": self.username,
            "is_typing": is_typing,
            "timestamp": time.time()
        }
        data_bytes = json.dumps(message_data).encode("utf-8")
        try:
            if recipient_address:
                self.p2p_manager.send_data(recipient_address, data_bytes)
            else: 
                if not self.p2p_manager.connections: return
                for peer_addr in list(self.p2p_manager.connections.keys()):
                    self.p2p_manager.send_data(peer_addr, data_bytes)
        except Exception as e:
            logger.error(f"Error sending typing status: {e}")

    def send_read_receipt(self, recipient_address, original_message_id):
        receipt_data = {
            "type": "chat_read_receipt",
            "message_id": original_message_id, # ID of the message being acknowledged
            "reader_username": self.username,
            "timestamp": time.time()
        }
        try:
            data_bytes = json.dumps(receipt_data).encode("utf-8")
            self.p2p_manager.send_data(recipient_address, data_bytes)
            # logger.info(f"Sent read receipt for {original_message_id} to {recipient_address}")
        except Exception as e:
            logger.error(f"Error sending read receipt: {e}")

# Example Usage (Conceptual)
if __name__ == "__main__":
    class MockP2PManager:
        def __init__(self):
            self.on_data_received = None; self.connections = {}; self.host_ip = "127.0.0.1"
        def send_data(self, _, data): print(f"MockP2P: Sending {data.decode()}"); return True
        def add_connection(self, addr): self.connections[addr] = "mock_socket"

    def gui_update(msg, _, is_self=False, message_id=None): print(f"GUI Update: {msg} (self: {is_self}, id: {message_id})")
    def gui_typing(usr, ip, is_typing): print(f"GUI Typing: {usr} ({ip}) is {'typing' if is_typing else 'idle'}.")
    def gui_read(reader, ip, msg_id): print(f"GUI Read Receipt: {msg_id} read by {reader} ({ip})")

    mock_p2p = MockP2PManager()
    chat_mgr = ChatManager("Alice", mock_p2p, gui_update, gui_typing, gui_read)
    bob_addr = ("192.168.1.101", 50001)

    print("--- Alice sends message to Bob ---")
    _, sent_msg_id = chat_mgr.send_chat_message(bob_addr, "Hello Bob, can you see this?")
    print(f"Alice sent message with ID: {sent_msg_id}")

    print("\n--- Simulating Bob receiving Alice's message and sending receipt ---")
    # Bob's app would call chat_mgr._handle_incoming_data with Alice's message
    # For this test, we manually simulate Bob's ChatManager receiving it and sending a receipt.
    # Let's assume Bob's ChatManager would then call send_read_receipt back to Alice.
    # Here, we directly simulate Alice receiving Bob's read receipt.
    read_receipt_from_bob = {
        "type": "chat_read_receipt",
        "message_id": sent_msg_id, # Bob acknowledges Alice's message ID
        "reader_username": "Bob",
        "timestamp": time.time()
    }
    chat_mgr._handle_incoming_data(bob_addr, json.dumps(read_receipt_from_bob).encode("utf-8"))

    print("\n--- Simulating Alice receiving a message from Charlie (and auto-sending receipt) ---")
    charlie_addr = ("192.168.1.102", 50001)
    charlie_msg_id = "msg_charlie_test123"
    msg_from_charlie = {
        "type": "chat_message",
        "message_id": charlie_msg_id,
        "sender": "Charlie",
        "content": "Hi Alice, testing read receipts!",
        "timestamp": time.time()
    }
    # When Alice's ChatManager handles this, it should automatically send a receipt to Charlie.
    # The mock_p2p.send_data will print the receipt being sent.
    chat_mgr._handle_incoming_data(charlie_addr, json.dumps(msg_from_charlie).encode("utf-8"))

