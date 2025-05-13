import socket
import threading
import json

class P2PConnectionManager:
    def __init__(self, host_ip, tcp_port=50001):
        self.host_ip = host_ip # The IP address of this machine
        self.tcp_port = tcp_port
        self.running = False
        self.server_socket = None
        self.connections = {}  # {("ip", port): socket_object}
        self.listener_thread = None

        # Callbacks for higher-level modules (Chat, File Transfer)
        self.on_peer_connected = None
        self.on_peer_disconnected = None
        self.on_data_received = None # This will likely be handled by specific handlers for chat/file
        self.on_connection_error = None

    def _listen_for_connections(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.server_socket.bind((self.host_ip, self.tcp_port))
            self.server_socket.listen(5) # Allow up to 5 queued connections
            print(f"P2P Manager: Listening for TCP connections on {self.host_ip}:{self.tcp_port}")
        except OSError as e:
            print(f"P2P Manager: Error binding TCP server socket: {e}. Port {self.tcp_port} might be in use.")
            if self.on_connection_error:
                self.on_connection_error(None, f"Failed to start TCP server: {e}")
            self.running = False
            return

        while self.running:
            try:
                conn, addr = self.server_socket.accept()
                print(f"P2P Manager: Accepted connection from {addr}")
                # We might want to authenticate or get username here before fully adding
                # For now, just add to connections and start a handler thread
                self.connections[addr] = conn
                if self.on_peer_connected:
                    self.on_peer_connected(addr, conn) # Notify that a raw connection is made
                
                # A dedicated thread to handle this specific client connection
                # This handler will be responsible for reading data and passing it to chat/file modules
                # For now, this basic structure just establishes the connection.
                # Data handling will be more complex.
                # client_handler_thread = threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True)
                # client_handler_thread.start()
            except socket.timeout: # server_socket.settimeout() would be needed for this
                continue
            except OSError as e:
                if self.running: # Only print error if we are supposed to be running
                    print(f"P2P Manager: Error accepting connections: {e}")
                break # Exit loop if socket error occurs (e.g. socket closed)
        
        if self.server_socket:
            self.server_socket.close()
        print("P2P Manager: Listener thread stopped.")

    # This method will be expanded significantly by ChatManager and FileTransferManager
    # def _handle_client(self, client_socket, client_address):
    #     try:
    #         while self.running:
    #             # This is a placeholder. Actual data handling will be more structured (e.g., JSON messages)
    #             data = client_socket.recv(4096) # Buffer size
    #             if not data:
    #                 print(f"P2P Manager: Connection closed by {client_address}")
    #                 break
    #             if self.on_data_received:
    #                 self.on_data_received(client_address, data)
    #     except ConnectionResetError:
    #         print(f"P2P Manager: Connection reset by {client_address}")
    #     except Exception as e:
    #         print(f"P2P Manager: Error handling client {client_address}: {e}")
    #     finally:
    #         self._cleanup_connection(client_address, client_socket)

    def _cleanup_connection(self, peer_address, peer_socket):
        if peer_socket:
            try:
                peer_socket.close()
            except Exception as e:
                print(f"P2P Manager: Error closing socket for {peer_address}: {e}")
        if peer_address in self.connections:
            del self.connections[peer_address]
            print(f"P2P Manager: Removed connection with {peer_address}")
            if self.on_peer_disconnected:
                self.on_peer_disconnected(peer_address)

    def connect_to_peer(self, peer_ip, peer_tcp_port):
        if (peer_ip, peer_tcp_port) in self.connections:
            print(f"P2P Manager: Already connected to {peer_ip}:{peer_tcp_port}")
            return self.connections[(peer_ip, peer_tcp_port)]
        try:
            peer_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            peer_socket.settimeout(5) # 5 second timeout for connection attempt
            peer_socket.connect((peer_ip, peer_tcp_port))
            peer_socket.settimeout(None) # Reset timeout after connection
            print(f"P2P Manager: Successfully connected to {peer_ip}:{peer_tcp_port}")
            self.connections[(peer_ip, peer_tcp_port)] = peer_socket
            if self.on_peer_connected:
                self.on_peer_connected((peer_ip, peer_tcp_port), peer_socket)
            
            # Start a handler for this connection as well if needed, or let higher modules manage
            # client_handler_thread = threading.Thread(target=self._handle_client, args=(peer_socket, (peer_ip, peer_tcp_port)), daemon=True)
            # client_handler_thread.start()
            return peer_socket
        except socket.timeout:
            print(f"P2P Manager: Timeout connecting to {peer_ip}:{peer_tcp_port}")
            if self.on_connection_error:
                self.on_connection_error((peer_ip, peer_tcp_port), "Connection timed out")
            return None
        except ConnectionRefusedError:
            print(f"P2P Manager: Connection refused by {peer_ip}:{peer_tcp_port}")
            if self.on_connection_error:
                self.on_connection_error((peer_ip, peer_tcp_port), "Connection refused")
            return None
        except Exception as e:
            print(f"P2P Manager: Error connecting to {peer_ip}:{peer_tcp_port}: {e}")
            if self.on_connection_error:
                self.on_connection_error((peer_ip, peer_tcp_port), str(e))
            return None

    def disconnect_from_peer(self, peer_ip, peer_tcp_port):
        peer_address = (peer_ip, peer_tcp_port)
        if peer_address in self.connections:
            peer_socket = self.connections[peer_address]
            self._cleanup_connection(peer_address, peer_socket)
        else:
            print(f"P2P Manager: Not connected to {peer_address}")

    def send_data(self, peer_address, data):
        if peer_address in self.connections:
            peer_socket = self.connections[peer_address]
            try:
                # Data should be bytes. Higher-level modules will handle serialization (e.g., JSON for messages)
                peer_socket.sendall(data)
                return True
            except socket.error as e:
                print(f"P2P Manager: Error sending data to {peer_address}: {e}")
                self._cleanup_connection(peer_address, peer_socket) # Assume connection is broken
                return False
        else:
            print(f"P2P Manager: Not connected to {peer_address}. Cannot send data.")
            return False

    def start(self):
        if self.running:
            print("P2P Manager: Service already running.")
            return
        self.running = True
        self.listener_thread = threading.Thread(target=self._listen_for_connections, daemon=True)
        self.listener_thread.start()

    def stop(self):
        if not self.running:
            print("P2P Manager: Service not running.")
            return
        self.running = False
        # Close server socket to unblock accept()
        if self.server_socket:
            try:
                # To unblock self.server_socket.accept(), we can connect to it briefly
                # This is a common trick for stopping blocking server sockets.
                dummy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                dummy_socket.settimeout(0.5)
                dummy_socket.connect((self.host_ip if self.host_ip != "0.0.0.0" else "127.0.0.1", self.tcp_port))
                dummy_socket.close()
            except Exception as e:
                # print(f"P2P Manager: Dummy socket connection to unblock server: {e}")
                pass # This might fail if server already closing, which is fine
            # self.server_socket.close() # This is done in the listener thread now

        # Close all active client connections
        for peer_addr, sock in list(self.connections.items()): # Iterate over a copy
            self._cleanup_connection(peer_addr, sock)
        
        if self.listener_thread and self.listener_thread.is_alive():
            self.listener_thread.join(timeout=2.0)
        print("P2P Manager: Service stopped.")

# Example Usage (for testing purposes)
if __name__ == "__main__":
    # Determine a local IP (this can be tricky; choose one for testing)
    # For testing, 0.0.0.0 is fine for server, but client needs a specific one.
    try:
        test_host_ip = socket.gethostbyname(socket.gethostname())
        # If gethostname() returns localhost or 127.0.0.1, try another way for LAN IP
        if test_host_ip.startswith("127."):
            # This is a common way but not foolproof
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            test_host_ip = s.getsockname()[0]
            s.close()
    except socket.gaierror:
        test_host_ip = "127.0.0.1" # Fallback
    print(f"Using host IP for P2P Manager: {test_host_ip}")

    manager1 = P2PConnectionManager(host_ip=test_host_ip, tcp_port=50001)
    manager2 = P2PConnectionManager(host_ip=test_host_ip, tcp_port=50002) # Run on different port for local test

    def on_connect(addr, sock):
        print(f"[CALLBACK] Connected to {addr}")
        # For testing, manager1 sends a message to manager2 upon connection
        if sock == manager1.connections.get(addr): # Check if this is the outgoing connection from manager1
            print("Manager1 sending hello to Manager2")
            manager1.send_data(addr, b"hello from manager1")

    def on_disconnect(addr):
        print(f"[CALLBACK] Disconnected from {addr}")

    def on_data(addr, data):
        print(f"[CALLBACK] Received from {addr}: {data.decode()}")
        # For testing, manager2 sends a reply
        if addr in manager2.connections: # Check if manager2 received this
            print("Manager2 sending world to Manager1")
            # This needs the address of manager1 from manager2's perspective
            # This simple test setup is a bit contrived for bidirectional send on connect.
            # Let's assume manager2 knows manager1's listening address if it connected to it.
            # For this test, we'll hardcode the reply target.
            manager2.send_data((test_host_ip, 50001), b"world from manager2")

    manager1.on_peer_connected = on_connect
    manager1.on_peer_disconnected = on_disconnect
    # manager1.on_data_received = on_data # Data handling will be more specific

    manager2.on_peer_connected = on_connect
    manager2.on_peer_disconnected = on_disconnect
    # manager2.on_data_received = on_data

    # To make on_data_received work in this test, we need a simple client handler
    # Let's add a simplified _handle_client to P2PConnectionManager for this test
    # (This would normally be part of ChatManager or FileTransferManager)
    def simple_handle_client(self, client_socket, client_address):
        try:
            while self.running:
                data = client_socket.recv(1024)
                if not data:
                    break
                print(f"P2P Manager ({self.tcp_port}) raw data from {client_address}: {data}")
                if self.on_data_received: # This is the P2PConnectionManager's callback
                    self.on_data_received(client_address, data)
        except ConnectionResetError:
            pass
        except Exception as e:
            print(f"Error in simple_handle_client for {client_address}: {e}")
        finally:
            self._cleanup_connection(client_address, client_socket)
    
    P2PConnectionManager._handle_client = simple_handle_client
    manager1.on_data_received = on_data
    manager2.on_data_received = on_data

    manager1.start()
    manager2.start()

    import time
    time.sleep(1)

    print(f"Manager1 attempting to connect to Manager2 ({test_host_ip}:50002)")
    conn_socket = manager1.connect_to_peer(test_host_ip, 50002)
    if conn_socket:
        print("Manager1 successfully initiated connection to Manager2.")
        # Start a handler for the outgoing connection on manager1's side
        # threading.Thread(target=manager1._handle_client, args=(conn_socket, (test_host_ip, 50002)), daemon=True).start()
    else:
        print("Manager1 failed to connect to Manager2.")

    try:
        while True:
            time.sleep(5)
            # print(f"Manager1 connections: {list(manager1.connections.keys())}")
            # print(f"Manager2 connections: {list(manager2.connections.keys())}")
    except KeyboardInterrupt:
        print("Stopping managers...")
    finally:
        manager1.stop()
        manager2.stop()

