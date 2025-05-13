import socket
import threading
import json
import logging

# Initialize logger
logger = logging.getLogger(__name__)

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
        """Listen for incoming P2P connections."""
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.server_socket.bind((self.host_ip, self.tcp_port))
                self.server_socket.listen(5) # Allow up to 5 queued connections
                logger.info(f"P2P Manager: Listening for TCP connections on {self.host_ip}:{self.tcp_port}")
            except OSError as e:
                logger.error(f"P2P Manager: Error binding TCP server socket: {e}. Port {self.tcp_port} might be in use.")
                if self.on_connection_error:
                    self.on_connection_error(None, f"Failed to start TCP server: {e}")
                self.running = False
                return

            while self.running:
                try:
                    conn, addr = self.server_socket.accept()
                    logger.info(f"P2P Manager: Accepted connection from {addr}")
                    self.connections[addr] = conn
                    if self.on_peer_connected:
                        self.on_peer_connected(addr, conn)
                except socket.timeout:
                    continue
                except OSError as e:
                    if self.running:
                        logger.error(f"P2P Manager: Error accepting connections: {e}")
                    break
        
        except socket.error as e:
            logger.error(f"Error setting up server socket: {e}")
        finally:
            if self.server_socket:
                self.server_socket.close()
            logger.info("P2P Manager: Listener thread stopped.")

    def _cleanup_connection(self, peer_address, peer_socket):
        if peer_socket:
            try:
                peer_socket.close()
            except Exception as e:
                logger.error(f"P2P Manager: Error closing socket for {peer_address}: {e}")
        if peer_address in self.connections:
            del self.connections[peer_address]
            logger.info(f"P2P Manager: Removed connection with {peer_address}")
            if self.on_peer_disconnected:
                self.on_peer_disconnected(peer_address)

    def connect_to_peer(self, peer_ip, peer_tcp_port):
        if (peer_ip, peer_tcp_port) in self.connections:
            logger.info(f"P2P Manager: Already connected to {peer_ip}:{peer_tcp_port}")
            return self.connections[(peer_ip, peer_tcp_port)]
        try:
            peer_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            peer_socket.settimeout(5) # 5 second timeout for connection attempt
            peer_socket.connect((peer_ip, peer_tcp_port))
            peer_socket.settimeout(None) # Reset timeout after connection
            logger.info(f"P2P Manager: Successfully connected to {peer_ip}:{peer_tcp_port}")
            self.connections[(peer_ip, peer_tcp_port)] = peer_socket
            if self.on_peer_connected:
                self.on_peer_connected((peer_ip, peer_tcp_port), peer_socket)
            return peer_socket
        except socket.timeout:
            logger.error(f"P2P Manager: Timeout connecting to {peer_ip}:{peer_tcp_port}")
            if self.on_connection_error:
                self.on_connection_error((peer_ip, peer_tcp_port), "Connection timed out")
            return None
        except ConnectionRefusedError:
            logger.error(f"P2P Manager: Connection refused by {peer_ip}:{peer_tcp_port}")
            if self.on_connection_error:
                self.on_connection_error((peer_ip, peer_tcp_port), "Connection refused")
            return None
        except Exception as e:
            logger.error(f"P2P Manager: Error connecting to {peer_ip}:{peer_tcp_port}: {e}")
            if self.on_connection_error:
                self.on_connection_error((peer_ip, peer_tcp_port), str(e))
            return None

    def disconnect_from_peer(self, peer_ip, peer_tcp_port):
        peer_address = (peer_ip, peer_tcp_port)
        if peer_address in self.connections:
            peer_socket = self.connections[peer_address]
            self._cleanup_connection(peer_address, peer_socket)
        else:
            logger.info(f"P2P Manager: Not connected to {peer_address}")

    def send_data(self, peer_address, data):
        if peer_address in self.connections:
            peer_socket = self.connections[peer_address]
            try:
                peer_socket.sendall(data)
                return True
            except socket.error as e:
                logger.error(f"P2P Manager: Error sending data to {peer_address}: {e}")
                self._cleanup_connection(peer_address, peer_socket)
                return False
        else:
            logger.info(f"P2P Manager: Not connected to {peer_address}. Cannot send data.")
            return False

    def start(self):
        if self.running:
            logger.info("P2P Manager: Service already running.")
            return
        self.running = True
        self.listener_thread = threading.Thread(target=self._listen_for_connections, daemon=True)
        self.listener_thread.start()

    def stop(self):
        if not self.running:
            logger.info("P2P Manager: Service not running.")
            return
        self.running = False
        if self.server_socket:
            try:
                dummy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                dummy_socket.settimeout(0.5)
                dummy_socket.connect((self.host_ip if self.host_ip != "0.0.0.0" else "127.0.0.1", self.tcp_port))
                dummy_socket.close()
            except Exception as e:
                pass
        for peer_addr, sock in list(self.connections.items()):
            self._cleanup_connection(peer_addr, sock)
        
        if self.listener_thread and self.listener_thread.is_alive():
            self.listener_thread.join(timeout=2.0)
        logger.info("P2P Manager: Service stopped.")

# Example Usage (for testing purposes)
if __name__ == "__main__":
    try:
        test_host_ip = socket.gethostbyname(socket.gethostname())
        if test_host_ip.startswith("127."):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            test_host_ip = s.getsockname()[0]
            s.close()
    except socket.gaierror:
        test_host_ip = "127.0.0.1"
    logger.info(f"Using host IP for P2P Manager: {test_host_ip}")

    manager1 = P2PConnectionManager(host_ip=test_host_ip, tcp_port=50001)
    manager2 = P2PConnectionManager(host_ip=test_host_ip, tcp_port=50002)

    def on_connect(addr, sock):
        logger.info(f"[CALLBACK] Connected to {addr}")
        if sock == manager1.connections.get(addr):
            logger.info("Manager1 sending hello to Manager2")
            manager1.send_data(addr, b"hello from manager1")

    def on_disconnect(addr):
        logger.info(f"[CALLBACK] Disconnected from {addr}")

    def on_data(addr, data):
        logger.info(f"[CALLBACK] Received from {addr}: {data.decode()}")
        if addr in manager2.connections:
            logger.info("Manager2 sending world to Manager1")
            manager2.send_data((test_host_ip, 50001), b"world from manager2")

    manager1.on_peer_connected = on_connect
    manager1.on_peer_disconnected = on_disconnect

    manager2.on_peer_connected = on_connect
    manager2.on_peer_disconnected = on_disconnect

    def simple_handle_client(self, client_socket, client_address):
        try:
            while self.running:
                data = client_socket.recv(1024)
                if not data:
                    break
                logger.info(f"P2P Manager ({self.tcp_port}) raw data from {client_address}: {data}")
                if self.on_data_received:
                    self.on_data_received(client_address, data)
        except ConnectionResetError:
            pass
        except Exception as e:
            logger.error(f"Error in simple_handle_client for {client_address}: {e}")
        finally:
            self._cleanup_connection(client_address, client_socket)
    
    P2PConnectionManager._handle_client = simple_handle_client
    manager1.on_data_received = on_data
    manager2.on_data_received = on_data

    manager1.start()
    manager2.start()

    import time
    time.sleep(1)

    logger.info(f"Manager1 attempting to connect to Manager2 ({test_host_ip}:50002)")
    conn_socket = manager1.connect_to_peer(test_host_ip, 50002)
    if conn_socket:
        logger.info("Manager1 successfully initiated connection to Manager2.")
    else:
        logger.info("Manager1 failed to connect to Manager2.")

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Stopping managers...")
    finally:
        manager1.stop()
        manager2.stop()

