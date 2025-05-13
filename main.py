import sys
import os
import socket
import json
import time
import logging
import netifaces
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QTextEdit, QListWidget, QPushButton, QLabel, QSplitter, 
    QFileDialog, QMessageBox, QInputDialog, QListWidgetItem, QAction, QProgressBar
)
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QTimer # Added QTimer
# from PyQt5.QtGui import QIcon # For future use with icons

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from gui.main_window import MainWindow as BaseMainWindow
from network.discovery import DiscoveryService
from network.p2p_manager import P2PConnectionManager
from chat.chat_manager import ChatManager
from file_transfer.file_transfer_manager import FileTransferManager

# --- Configuration ---
DISCOVERY_PORT = 50000
TCP_PORT = 50001
BROADCAST_INTERVAL = 5
TYPING_TIMEOUT_MS = 2000 # 2 seconds before sending "not typing"

# --- Helper to get a suitable local IP ---
def get_local_ip():
    """Get a suitable local IP address for LAN communication."""
    try:
        interfaces = netifaces.interfaces()
        for iface in interfaces:
            # Skip loopback and virtual interfaces
            if iface.startswith(('lo', 'docker', 'veth', 'br-', 'vnet')):
                continue
            addrs = netifaces.ifaddresses(iface)
            if netifaces.AF_INET in addrs:
                for addr in addrs[netifaces.AF_INET]:
                    ip = addr['addr']
                    # Skip localhost, docker, and VPN addresses
                    if not ip.startswith(('127.', '172.', '10.')):
                        logger = logging.getLogger(__name__)
                        logger.info(f"Selected network interface: {iface} with IP: {ip}")
                        return ip
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error getting network interfaces: {e}")
        
    # Fallback to socket method
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return ip
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error getting IP via socket: {e}")
        return "127.0.0.1"
    finally:
        s.close()

# --- Communication signals for cross-thread GUI updates ---
class GuiUpdater(QObject):
    # Chat and messaging signals
    update_chat_display = pyqtSignal(str)
    update_message_status = pyqtSignal(str, str)  # message_id, status (sent/delivered/read)
    update_typing_indicator = pyqtSignal(str)
    
    # User management signals
    add_user_to_list = pyqtSignal(str, str, dict)  # username, ip, additional_info
    remove_user_from_list = pyqtSignal(str)
    update_user_status = pyqtSignal(str, str)  # ip, status (online/away/busy)
    
    # File transfer signals
    update_file_transfer_status = pyqtSignal(str, str, str, float)  # id, status, message, progress
    update_file_transfer_speed = pyqtSignal(str, float)  # id, speed_kbps
    update_transfer_progress = pyqtSignal(str, float, float)  # id, progress_percent, remaining_time_sec
    prompt_user_for_file = pyqtSignal(str, object, str, str, int, dict)  # sender, peer_addr, file_id, filename, size, metadata
    
    # Network status signals
    update_network_status = pyqtSignal(str, bool)  # interface, is_connected
    update_connection_quality = pyqtSignal(str, int)  # peer_ip, latency_ms
    update_bandwidth_usage = pyqtSignal(float, float)  # upload_kbps, download_kbps
    update_connection_stats = pyqtSignal(str, dict)  # peer_ip, stats_dict
    
    # Error handling signals
    show_error = pyqtSignal(str, str, bool)  # title, message, is_critical
    show_warning = pyqtSignal(str, str)  # title, message
    show_network_error = pyqtSignal(str, str, bool)  # interface, error_msg, is_critical
    show_transfer_error = pyqtSignal(str, str, str)  # file_id, error_type, error_msg
    
    # Status bar signals
    update_status_message = pyqtSignal(str, int)  # message, timeout_ms
    clear_status_message = pyqtSignal()
    
    # Connection diagnostic signals
    update_peer_diagnostics = pyqtSignal(str, dict)  # peer_ip, diagnostic_info
    update_network_metrics = pyqtSignal(dict)  # metrics_dict with various network stats

class LanMessengerApp(BaseMainWindow):
    def __init__(self, username, local_ip):
        super().__init__()
        self.username = username
        self.local_ip = local_ip
        self.gui_updater = GuiUpdater()
        self.current_typing_users = {} # {ip: username}
        self.DEFAULT_DOWNLOAD_DIR = "/home/amiral/Downloads/lan_messenger_app/LanMessengerDownloads"

        if not os.path.exists(self.DEFAULT_DOWNLOAD_DIR):
            try: os.makedirs(self.DEFAULT_DOWNLOAD_DIR)
            except OSError as e:
                print(f"Error creating download directory {DEFAULT_DOWNLOAD_DIR}: {e}")
                alt_download_dir = os.path.join(os.getcwd(), "downloads")
                if not os.path.exists(alt_download_dir):
                    try: os.makedirs(alt_download_dir)
                    except OSError as e_alt:
                        print(f"Error creating alternative download directory {alt_download_dir}: {e_alt}")
                        alt_download_dir = "."
                DEFAULT_DOWNLOAD_DIR = alt_download_dir
        self.download_dir = self.DEFAULT_DOWNLOAD_DIR

        self._setup_core_services()
        self._connect_signals_and_actions()
        self._setup_typing_indicator_logic()
        self._start_services()
        self.user_list_widget.clear()
        my_item = QListWidgetItem(f"{self.username} (You) - {self.local_ip}")
        my_item.setForeground(Qt.blue)
        self.user_list_widget.addItem(my_item)
        self.online_users = {self.local_ip: self.username}

    def _setup_core_services(self):
        self.discovery_service = DiscoveryService(username=self.username, discovery_port=DISCOVERY_PORT, broadcast_interval=BROADCAST_INTERVAL)
        self.p2p_manager = P2PConnectionManager(host_ip=self.local_ip, tcp_port=TCP_PORT)
        self.chat_manager = ChatManager(username=self.username, 
                                        p2p_manager=self.p2p_manager, 
                                        gui_update_callback=self.handle_chat_update,
                                        gui_typing_status_callback=self.handle_incoming_typing_status,
                                        gui_read_receipt_callback=self.handle_incoming_read_receipt)
        self.file_transfer_manager = FileTransferManager(username=self.username,
                                                       p2p_manager=self.p2p_manager,
                                                       gui_update_callback=self.handle_file_transfer_update,
                                                       gui_file_prompt_callback=self.handle_file_prompt,
                                                       default_download_path=self.download_dir)

        self.p2p_manager.on_data_received = self._route_incoming_p2p_data
        self.p2p_manager.on_peer_connected = self.handle_peer_connected
        self.p2p_manager.on_peer_disconnected = self.handle_peer_disconnected
        self.p2p_manager.on_connection_error = self.handle_connection_error

        self.discovery_service.on_user_discovered = self.handle_user_discovered
        self.discovery_service.on_user_lost = self.handle_user_lost

    def _connect_signals_and_actions(self):
        # Connect basic GUI update signals
        self.gui_updater.update_chat_display.connect(self.append_to_chat_display)
        self.gui_updater.add_user_to_list.connect(self.add_user_gui)
        self.gui_updater.remove_user_from_list.connect(self.remove_user_gui)
        self.gui_updater.update_typing_indicator.connect(self.update_typing_indicator_label)
        
        # Connect file transfer signals
        self.gui_updater.update_file_transfer_status.connect(self.update_file_transfer_gui)
        self.gui_updater.update_file_transfer_speed.connect(self.update_transfer_speed_gui)
        self.gui_updater.update_transfer_progress.connect(self.update_transfer_progress_gui)
        self.gui_updater.prompt_user_for_file.connect(self.prompt_user_for_file_gui)
        
        # Connect status and error signals
        self.gui_updater.show_error.connect(self.show_error_dialog)
        self.gui_updater.show_warning.connect(self.show_warning_dialog)
        self.gui_updater.show_network_error.connect(self.show_network_error_gui)
        self.gui_updater.show_transfer_error.connect(self.show_transfer_error_gui)
        
        # Connect network status signals
        self.gui_updater.update_network_status.connect(self.update_network_status_gui)
        self.gui_updater.update_connection_quality.connect(self.update_connection_quality_gui)
        self.gui_updater.update_bandwidth_usage.connect(self.update_bandwidth_usage_gui)
        self.gui_updater.update_connection_stats.connect(self.update_connection_stats_gui)
        
        # Connect diagnostic signals
        self.gui_updater.update_peer_diagnostics.connect(self.update_peer_diagnostics_gui)
        self.gui_updater.update_network_metrics.connect(self.update_network_metrics_gui)
        
        # Connect user status signals
        self.gui_updater.update_user_status.connect(self.update_user_status_gui)
        self.gui_updater.update_message_status.connect(self.update_message_status_gui)
        
        # Connect button actions
        if hasattr(self, 'send_message_button'):
            self.send_message_button.clicked.connect(self.send_message_action)
        if hasattr(self, 'attach_file_button'):
            self.attach_file_button.clicked.connect(self.attach_file_action)
        
        self.user_list_widget.itemDoubleClicked.connect(self.initiate_connection_from_list)
        self.message_input_field.textChanged.connect(self.handle_message_input_text_changed)
        
        # Connect menu actions
        for action in self.menuBar().findChildren(QAction):
            if action.objectName() == "settings_action": 
                action.triggered.connect(self.open_settings_dialog)
            elif action.objectName() == "exit_action": 
                action.triggered.connect(self.close)
            elif action.objectName() == "about_action": 
                action.triggered.connect(self.show_about_dialog)

    def _setup_typing_indicator_logic(self):
        self.typing_timer = QTimer(self)
        self.typing_timer.setSingleShot(True)
        self.typing_timer.setInterval(TYPING_TIMEOUT_MS)
        self.typing_timer.timeout.connect(self.send_stopped_typing_status)
        self.am_i_typing = False

    def _start_services(self):
        self.p2p_manager.start()
        self.discovery_service.start()
        self.append_to_chat_display(f"[System] Welcome, {self.username}! Listening on {self.local_ip}:{TCP_PORT}. Downloads to: {self.download_dir}")

    def _route_incoming_p2p_data(self, peer_address_tuple, data_bytes):
        try:
            message_str = data_bytes.decode("utf-8")
            message_data = json.loads(message_str)
            msg_type = message_data.get("type", "unknown")

            if msg_type.startswith("chat_"):
                self.chat_manager._handle_incoming_data(peer_address_tuple, data_bytes)
            elif msg_type.startswith("file_"):
                self.file_transfer_manager.handle_incoming_file_data(peer_address_tuple, data_bytes)
            else:
                self.file_transfer_manager._process_raw_file_chunk(peer_address_tuple, data_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.file_transfer_manager._process_raw_file_chunk(peer_address_tuple, data_bytes)
        except Exception as e:
            print(f"[Router] Error routing data from {peer_address_tuple}: {e}")
            self.gui_updater.update_chat_display.emit(f"[Error] Processing data from {peer_address_tuple[0]}: {e}")

    def append_to_chat_display(self, message):
        self.chat_display_area.append(message)
        self.chat_display_area.verticalScrollBar().setValue(self.chat_display_area.verticalScrollBar().maximum())

    def add_user_gui(self, username, ip_address):
        if ip_address == self.local_ip: return
        for i in range(self.user_list_widget.count()):
            item = self.user_list_widget.item(i)
            if ip_address in item.text():
                item.setText(f"{username} - {ip_address}")
                self.online_users[ip_address] = username
                return
        item = QListWidgetItem(f"{username} - {ip_address}")
        self.user_list_widget.addItem(item)
        self.online_users[ip_address] = username
        self.append_to_chat_display(f"[System] {username} ({ip_address}) is online.")

    def remove_user_gui(self, ip_address):
        removed_username = self.online_users.pop(ip_address, ip_address)
        if ip_address in self.current_typing_users: # Remove from typing list if they go offline
            del self.current_typing_users[ip_address]
            self._update_displayed_typing_status()
        for i in range(self.user_list_widget.count()):
            item = self.user_list_widget.item(i)
            if item and ip_address in item.text():
                self.user_list_widget.takeItem(i)
                self.append_to_chat_display(f"[System] {removed_username} ({ip_address}) went offline.")
                break

    def update_file_transfer_gui(self, file_id, status, message):
        self.file_transfer_area.append(f"ID:{file_id} | {status} | {message}")
        self.file_transfer_area.verticalScrollBar().setValue(self.file_transfer_area.verticalScrollBar().maximum())

    def prompt_user_for_file_gui(self, sender_username, peer_address_tuple, file_id, filename, filesize_bytes):
        filesize_kb = filesize_bytes / 1024.0
        reply = QMessageBox.question(self, "Incoming File Transfer",
                                     f"{sender_username} ({peer_address_tuple[0]}) wants to send you:\n\n"
                                     f"File: {filename}\nSize: {filesize_kb:.2f} KB\n\n"
                                     f"Accept this file? (Saves to {self.download_dir})",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.file_transfer_manager.accept_file_transfer(peer_address_tuple, file_id, filename, filesize_bytes)
        else:
            self.file_transfer_manager.reject_file_transfer(peer_address_tuple, file_id)
    
    def update_typing_indicator_label(self, text):
        self.typing_indicator_label.setText(text)

    def handle_chat_update(self, message, peer_address_tuple=None, is_self=False, message_id=None):
        self.gui_updater.update_chat_display.emit(message)
        # If it's an incoming message from someone else, they are no longer typing that message
        if peer_address_tuple and not is_self and peer_address_tuple[0] in self.current_typing_users:
            del self.current_typing_users[peer_address_tuple[0]]
            self._update_displayed_typing_status()

    def handle_user_discovered(self, username, ip_address):
        if ip_address != self.local_ip:
            self.gui_updater.add_user_to_list.emit(username, ip_address)
            if (ip_address, TCP_PORT) not in self.p2p_manager.connections:
                self.p2p_manager.connect_to_peer(ip_address, TCP_PORT)

    def handle_user_lost(self, username, ip_address):
        self.gui_updater.remove_user_from_list.emit(ip_address)
        if (ip_address, TCP_PORT) in self.p2p_manager.connections:
            self.p2p_manager.disconnect_from_peer(ip_address, TCP_PORT)

    def handle_file_transfer_update(self, file_id, status, message):
        self.gui_updater.update_file_transfer_status.emit(file_id, status, message)

    def handle_file_prompt(self, sender_username, peer_address_tuple, file_id, filename, filesize):
        self.gui_updater.prompt_user_for_file.emit(sender_username, peer_address_tuple, file_id, filename, filesize)

    def handle_peer_connected(self, peer_address_tuple, peer_socket):
        peer_ip = peer_address_tuple[0]
        username = self.online_users.get(peer_ip, peer_ip)
        self.append_to_chat_display(f"[System] TCP Connection established with {username} ({peer_ip}).")
        found_in_gui = any(peer_ip in self.user_list_widget.item(i).text() for i in range(self.user_list_widget.count()))
        if not found_in_gui and peer_ip != self.local_ip:
            self.gui_updater.add_user_to_list.emit(username, peer_ip)

    def handle_peer_disconnected(self, peer_address_tuple):
        peer_ip = peer_address_tuple[0]
        username = self.online_users.get(peer_ip, peer_ip)
        self.append_to_chat_display(f"[System] TCP Connection lost with {username} ({peer_ip}).")
        if peer_ip in self.current_typing_users:
            del self.current_typing_users[peer_ip]
            self._update_displayed_typing_status()

    def handle_connection_error(self, peer_address_tuple, error_message):
        error_msg_prefix = "[Error]"
        if peer_address_tuple:
            peer_ip = peer_address_tuple[0]
            username = self.online_users.get(peer_ip, peer_ip)
            error_msg_prefix = f"[Error] Connection to {username} ({peer_ip}) failed:"
        else:
            error_msg_prefix = f"[Error] Network connection error:"
        self.append_to_chat_display(f"{error_msg_prefix} {error_message}")
        if not peer_address_tuple: QMessageBox.critical(self, "Network Error", f"A critical network error occurred: {error_message}.")

    def handle_message_input_text_changed(self):
        if not self.am_i_typing:
            self.am_i_typing = True
            self._broadcast_typing_status(True)
        self.typing_timer.start() # Restart timer

    def send_stopped_typing_status(self):
        if self.am_i_typing:
            self.am_i_typing = False
            self._broadcast_typing_status(False)

    def _broadcast_typing_status(self, is_typing):
        # Send to selected user if one is selected for DM, otherwise broadcast to all connected
        selected_items = self.user_list_widget.selectedItems()
        recipient_ip = None
        if selected_items:
            selected_text = selected_items[0].text()
            if "(You)" not in selected_text:
                try: recipient_ip = selected_text.split(" - ")[-1].strip()
                except: pass
        
        if recipient_ip and recipient_ip != self.local_ip:
            self.chat_manager.send_typing_status((recipient_ip, TCP_PORT), is_typing)
        else: # Broadcast to all connected peers
            self.chat_manager.send_typing_status(None, is_typing)

    def handle_incoming_typing_status(self, sender_username, sender_ip, is_typing):
        if sender_ip == self.local_ip: return # Ignore self

        if is_typing:
            self.current_typing_users[sender_ip] = sender_username
        elif sender_ip in self.current_typing_users:
            del self.current_typing_users[sender_ip]
        self._update_displayed_typing_status()

    def _update_displayed_typing_status(self):
        typing_users_list = [name for name in self.current_typing_users.values()]
        if not typing_users_list:
            self.gui_updater.update_typing_indicator.emit("")
        elif len(typing_users_list) == 1:
            self.gui_updater.update_typing_indicator.emit(f"{typing_users_list[0]} is typing...")
        elif len(typing_users_list) == 2:
            self.gui_updater.update_typing_indicator.emit(f"{typing_users_list[0]} and {typing_users_list[1]} are typing...")
        else:
            self.gui_updater.update_typing_indicator.emit(f"{typing_users_list[0]} and {len(typing_users_list)-1} others are typing...")

    def handle_incoming_read_receipt(self, reader_username, reader_ip, original_message_id):
        # This is where GUI would be updated to show "Read by UserX" or a checkmark
        # For now, just print to chat display for confirmation
        self.gui_updater.update_chat_display.emit(f"[System] Message {original_message_id[:8]}... read by {reader_username} ({reader_ip}).")

    def send_message_action(self):
        message = self.message_input_field.toPlainText().strip()
        if not message: return
        self.send_stopped_typing_status() # Stop typing status when message is sent

        selected_items = self.user_list_widget.selectedItems()
        recipient_ip = None
        if selected_items:
            selected_text = selected_items[0].text()
            if "(You)" not in selected_text:
                try: recipient_ip = selected_text.split(" - ")[-1].strip()
                except: pass
        
        if recipient_ip and recipient_ip != self.local_ip:
            recipient_address = (recipient_ip, TCP_PORT)
            success, msg_id = self.chat_manager.send_chat_message(recipient_address, message)
            if success:
                self.append_to_chat_display(f"{time.strftime('%H:%M:%S')} You to {self.online_users.get(recipient_ip, recipient_ip)}: {message} (ID: {msg_id[:8]}...)")
                self.message_input_field.clear()
            else:
                self.append_to_chat_display(f"[Error] Failed to send DM to {recipient_ip}.")
        else:
            success, msg_id = self.chat_manager.send_group_chat_message(message)
            if success:
                self.message_input_field.clear()
            else:
                self.append_to_chat_display("[Error] Failed to send group message. No peers connected?")

    def attach_file_action(self):
        selected_items = self.user_list_widget.selectedItems()
        recipient_ip = None
        if selected_items:
            selected_text = selected_items[0].text()
            if "(You)" not in selected_text:
                try: recipient_ip = selected_text.split(" - ")[-1].strip()
                except: pass
        
        if not recipient_ip or recipient_ip == self.local_ip:
            QMessageBox.information(self, "Select User", "Please select a user from the list to send a file to (cannot send to self).")
            return

        filepath, _ = QFileDialog.getOpenFileName(self, "Select File to Send")
        if filepath:
            recipient_address = (recipient_ip, TCP_PORT)
            if recipient_address not in self.p2p_manager.connections:
                self.append_to_chat_display(f"[System] Not directly connected to {recipient_ip}. Attempting to connect...")
                if not self.p2p_manager.connect_to_peer(recipient_ip, TCP_PORT):
                    self.append_to_chat_display(f"[Error] Could not connect to {recipient_ip} to send file.")
                    return
                time.sleep(0.5)
            self.file_transfer_manager.offer_file(recipient_address, filepath)
        else:
            self.append_to_chat_display("[System] File selection cancelled.")

    def initiate_connection_from_list(self, item):
        selected_text = item.text()
        if "(You)" in selected_text: return
        try:
            ip_address = selected_text.split(" - ")[-1].strip()
            if (ip_address, TCP_PORT) not in self.p2p_manager.connections:
                self.append_to_chat_display(f"[System] Attempting to connect to {ip_address}...")
                self.p2p_manager.connect_to_peer(ip_address, TCP_PORT)
            else:
                self.append_to_chat_display(f"[System] Already connected to {ip_address}.")
        except Exception as e:
            self.append_to_chat_display(f"[Error] Could not parse IP from selection: {e}")

    def open_settings_dialog(self):
        download_dir, ok = QInputDialog.getText(self, "Settings", "Enter new download directory:", text=self.download_dir)
        if ok and download_dir:
            if os.path.isdir(os.path.dirname(download_dir)) or os.path.isdir(download_dir):
                if not os.path.exists(download_dir):
                    try: os.makedirs(download_dir)
                    except Exception as e:
                        QMessageBox.warning(self, "Settings Error", f"Could not create directory {download_dir}: {e}"); return
                self.download_dir = download_dir
                self.file_transfer_manager.default_download_path = download_dir
                self.append_to_chat_display(f"[System] Download directory changed to: {self.download_dir}")
            else:
                QMessageBox.warning(self, "Settings Error", f"Invalid directory path: {download_dir}")

    def show_about_dialog(self):
        QMessageBox.about(self, "About LAN Messenger", "LAN Messenger & File Sharer\n\nVersion 0.2 Alpha\nCreated by Manus AI Agent.")

    def closeEvent(self, event):
        self.append_to_chat_display("[System] Shutting down services...")
        self.send_stopped_typing_status() # Notify others we stopped typing
        if self.discovery_service: self.discovery_service.stop()
        if self.p2p_manager: self.p2p_manager.stop()
        self.append_to_chat_display("[System] Goodbye!")
        event.accept()

    def _handle_error(self, title: str, message: str, is_critical: bool = False) -> None:
        """Handle errors in a consistent way across the application."""
        self.gui_updater.show_error.emit(title, message, is_critical)
        self.append_to_chat_display(f"[Error] {message}")
        
    def _handle_warning(self, title: str, message: str) -> None:
        """Handle warnings in a consistent way."""
        self.gui_updater.show_warning.emit(title, message)
        self.append_to_chat_display(f"[Warning] {message}")

    def _update_network_status(self, interface: str, is_connected: bool) -> None:
        """Update the network status in the GUI."""
        self.gui_updater.update_network_status.emit(interface, is_connected)
        status = "connected" if is_connected else "disconnected"
        self.append_to_chat_display(f"[Network] Interface {interface} is {status}")

    def _update_connection_quality(self, peer_ip: str, latency_ms: int) -> None:
        """Update connection quality information."""
        self.gui_updater.update_connection_quality.emit(peer_ip, latency_ms)
        username = self.online_users.get(peer_ip, peer_ip)
        if latency_ms > 1000:  # High latency warning
            self._handle_warning("High Latency", 
                f"Connection to {username} is experiencing high latency ({latency_ms}ms)")

    def _update_message_status(self, message_id: str, status: str) -> None:
        """Update message delivery/read status."""
        self.gui_updater.update_message_status.emit(message_id, status)
        if status == "failed":
            self._handle_warning("Message Failed", 
                f"Failed to deliver message (ID: {message_id[:8]}...)")

    def _update_file_transfer_progress(self, file_id: str, status: str, 
                                     message: str, progress: float = 0.0, 
                                     speed_kbps: float = 0.0) -> None:
        """Update file transfer progress with detailed information."""
        self.gui_updater.update_file_transfer_status.emit(file_id, status, message, progress)
        if speed_kbps > 0:
            self.gui_updater.update_file_transfer_speed.emit(file_id, speed_kbps)
        
        # Show warnings for slow transfers
        if status == "transferring" and speed_kbps < 50:  # Less than 50 KB/s
            self._handle_warning("Slow Transfer", 
                f"File transfer is running slowly ({speed_kbps:.1f} KB/s)")

    def _update_user_status(self, ip_address: str, status: str) -> None:
        """Update user online status."""
        self.gui_updater.update_user_status.emit(ip_address, status)
        username = self.online_users.get(ip_address, ip_address)
        if status == "away":
            self.append_to_chat_display(f"[Status] {username} is away")

    def show_error_dialog(self, title: str, message: str, is_critical: bool = False) -> None:
        """Show an error dialog to the user."""
        if is_critical:
            QMessageBox.critical(self, title, message)
        else:
            QMessageBox.warning(self, title, message)

    def show_warning_dialog(self, title: str, message: str) -> None:
        """Show a warning dialog to the user."""
        QMessageBox.warning(self, title, message)

    def update_network_status_gui(self, interface: str, is_connected: bool) -> None:
        """Update network status in the status bar."""
        if is_connected:
            self.statusBar.showMessage(f"Connected on {interface}")
        else:
            self.statusBar.showMessage(f"Disconnected from {interface}", 5000)

    def update_connection_quality_gui(self, peer_ip: str, latency_ms: int) -> None:
        """Update connection quality indicator for a peer."""
        username = self.online_users.get(peer_ip, peer_ip)
        for i in range(self.user_list_widget.count()):
            item = self.user_list_widget.item(i)
            if peer_ip in item.text():
                # Add latency info to the user list item
                current_text = item.text().split(" [")[0]  # Remove any existing latency info
                if latency_ms > 1000:
                    item.setForeground(Qt.red)
                    item.setText(f"{current_text} [High Latency: {latency_ms}ms]")
                elif latency_ms > 500:
                    item.setForeground(Qt.yellow)
                    item.setText(f"{current_text} [Latency: {latency_ms}ms]")
                else:
                    item.setForeground(Qt.green)
                    item.setText(current_text)
                break

    def update_transfer_speed_gui(self, file_id: str, speed_kbps: float) -> None:
        """Update file transfer speed in the transfer area."""
        if speed_kbps >= 1024:
            speed_text = f"{speed_kbps/1024:.1f} MB/s"
        else:
            speed_text = f"{speed_kbps:.1f} KB/s"
        self.file_transfer_area.append(f"Transfer speed for {file_id}: {speed_text}")

    def update_message_status_gui(self, message_id: str, status: str) -> None:
        """Update message status in the chat display."""
        status_text = {
            "sent": "✓",
            "delivered": "✓✓",
            "read": "✓✓✓",
            "failed": "❌"
        }.get(status, "")
        if status_text:
            # Find the message in chat display and update its status
            chat_text = self.chat_display_area.toPlainText()
            if f"(ID: {message_id[:8]}...)" in chat_text:
                lines = chat_text.split("\n")
                for i, line in enumerate(lines):
                    if f"(ID: {message_id[:8]}...)" in line:
                        if "] " in line:  # Has existing status
                            lines[i] = line.split("] ")[0] + f"] {status_text} " + line.split("] ")[-1]
                        else:
                            lines[i] = f"{line} [{status_text}]"
                        break
                self.chat_display_area.setPlainText("\n".join(lines))

    def update_transfer_progress_gui(self, file_id: str, progress_percent: float, remaining_time_sec: float) -> None:
        """Update detailed file transfer progress in the transfer area."""
        if remaining_time_sec > 60:
            time_str = f"{remaining_time_sec/60:.1f} minutes remaining"
        else:
            time_str = f"{remaining_time_sec:.0f} seconds remaining"
        
        self.file_transfer_area.append(
            f"Transfer {file_id}: {progress_percent:.1f}% complete - {time_str}")
        
        # Update progress bar if it exists for this transfer
        progress_bar = self.findChild(QProgressBar, f"progress_{file_id}")
        if progress_bar:
            progress_bar.setValue(int(progress_percent))
            progress_bar.setFormat(f"{progress_percent:.1f}% - {time_str}")

    def update_bandwidth_usage_gui(self, upload_kbps: float, download_kbps: float) -> None:
        """Update bandwidth usage indicators."""
        up_text = f"{upload_kbps/1024:.1f} MB/s" if upload_kbps >= 1024 else f"{upload_kbps:.1f} KB/s"
        down_text = f"{download_kbps/1024:.1f} MB/s" if download_kbps >= 1024 else f"{download_kbps:.1f} KB/s"
        self.statusBar().showMessage(f"↑ {up_text} | ↓ {down_text}")

    def update_connection_stats_gui(self, peer_ip: str, stats: dict) -> None:
        """Update detailed connection statistics for a peer."""
        username = self.online_users.get(peer_ip, peer_ip)
        stats_text = []
        
        if 'latency' in stats:
            latency = stats['latency']
            color = Qt.green if latency < 100 else Qt.yellow if latency < 500 else Qt.red
            stats_text.append(f"Latency: {latency}ms")
        
        if 'packet_loss' in stats:
            packet_loss = stats['packet_loss']
            if packet_loss > 0:
                stats_text.append(f"Packet Loss: {packet_loss:.1f}%")
        
        if 'bandwidth' in stats:
            bw = stats['bandwidth']
            stats_text.append(f"Bandwidth: {bw/1024:.1f} MB/s")
            
        # Update user list item with connection stats
        for i in range(self.user_list_widget.count()):
            item = self.user_list_widget.item(i)
            if peer_ip in item.text():
                base_text = item.text().split(" [")[0]
                item.setText(f"{base_text} [{' | '.join(stats_text)}]")
                if 'latency' in stats:
                    item.setForeground(color)
                break

    def show_network_error_gui(self, interface: str, error_msg: str, is_critical: bool) -> None:
        """Display network-specific errors."""
        title = "Critical Network Error" if is_critical else "Network Warning"
        message = f"Network interface {interface} encountered an issue:\n{error_msg}"
        
        if is_critical:
            QMessageBox.critical(self, title, message)
            self.statusBar().showMessage(f"Network Error on {interface}", 5000)
        else:
            QMessageBox.warning(self, title, message)
        
        self.append_to_chat_display(f"[Network Error] {interface}: {error_msg}")

    def show_transfer_error_gui(self, file_id: str, error_type: str, error_msg: str) -> None:
        """Display file transfer specific errors."""
        title = f"File Transfer Error: {error_type}"
        QMessageBox.warning(self, title, f"Transfer {file_id} failed:\n{error_msg}")
        self.file_transfer_area.append(f"[Error] Transfer {file_id}: {error_type} - {error_msg}")
        
        # Update any progress bars or status indicators
        progress_bar = self.findChild(QProgressBar, f"progress_{file_id}")
        if progress_bar:
            progress_bar.setFormat("Transfer Failed")
            progress_bar.setStyleSheet("QProgressBar::chunk { background-color: red; }")

    def update_peer_diagnostics_gui(self, peer_ip: str, diagnostic_info: dict) -> None:
        """Update comprehensive peer connection diagnostics."""
        username = self.online_users.get(peer_ip, peer_ip)
        
        # Create or update a detailed diagnostic display
        text = [f"Connection Diagnostics for {username} ({peer_ip}):"]
        
        if 'connection_uptime' in diagnostic_info:
            uptime = diagnostic_info['connection_uptime']
            text.append(f"Connection Uptime: {uptime:.1f} seconds")
            
        if 'avg_latency' in diagnostic_info:
            avg_latency = diagnostic_info['avg_latency']
            text.append(f"Average Latency: {avg_latency:.1f}ms")
            
        if 'packet_loss_rate' in diagnostic_info:
            loss_rate = diagnostic_info['packet_loss_rate']
            text.append(f"Packet Loss Rate: {loss_rate:.2f}%")
            
        if 'connection_quality' in diagnostic_info:
            quality = diagnostic_info['connection_quality']
            text.append(f"Connection Quality: {quality}")
            
        # Display in a dedicated diagnostic area or log
        if hasattr(self, 'diagnostic_area'):
            self.diagnostic_area.setText("\n".join(text))
        else:
            self.append_to_chat_display("\n".join(text))

    def update_network_metrics_gui(self, metrics: dict) -> None:
        """Update overall network performance metrics."""
        if not hasattr(self, 'network_metrics_label'):
            self.network_metrics_label = QLabel()
            self.statusBar().addPermanentWidget(self.network_metrics_label)
            
        # Format metrics into a concise display
        metrics_text = []
        
        if 'active_connections' in metrics:
            metrics_text.append(f"Connections: {metrics['active_connections']}")
            
        if 'total_bandwidth' in metrics:
            bw = metrics['total_bandwidth'] / 1024  # Convert to MB/s
            metrics_text.append(f"Bandwidth: {bw:.1f}MB/s")
            
        if 'avg_network_latency' in metrics:
            metrics_text.append(f"Avg Latency: {metrics['avg_network_latency']:.0f}ms")
            
        self.network_metrics_label.setText(" | ".join(metrics_text))

    def update_user_status_gui(self, ip_address: str, status: str) -> None:
        """Update user status in the user list."""
        for i in range(self.user_list_widget.count()):
            item = self.user_list_widget.item(i)
            if ip_address in item.text():
                username = self.online_users.get(ip_address, ip_address)
                status_icon = {
                    "online": "🟢",
                    "away": "🟡",
                    "busy": "🔴",
                    "offline": "⚫"
                }.get(status, "")
                item.setText(f"{status_icon} {username} - {ip_address}")
                break

    def update_transfer_progress_gui(self, file_id: str, progress_percent: float, remaining_time_sec: float) -> None:
        """Update detailed file transfer progress in the transfer area."""
        if remaining_time_sec > 60:
            time_str = f"{remaining_time_sec/60:.1f} minutes remaining"
        else:
            time_str = f"{remaining_time_sec:.0f} seconds remaining"
        
        self.file_transfer_area.append(
            f"Transfer {file_id}: {progress_percent:.1f}% complete - {time_str}")
        
        # Update progress bar if it exists for this transfer
        progress_bar = self.findChild(QProgressBar, f"progress_{file_id}")
        if progress_bar:
            progress_bar.setValue(int(progress_percent))
            progress_bar.setFormat(f"{progress_percent:.1f}% - {time_str}")

    def update_bandwidth_usage_gui(self, upload_kbps: float, download_kbps: float) -> None:
        """Update bandwidth usage indicators."""
        up_text = f"{upload_kbps/1024:.1f} MB/s" if upload_kbps >= 1024 else f"{upload_kbps:.1f} KB/s"
        down_text = f"{download_kbps/1024:.1f} MB/s" if download_kbps >= 1024 else f"{download_kbps:.1f} KB/s"
        self.statusBar().showMessage(f"↑ {up_text} | ↓ {down_text}")

    def update_connection_stats_gui(self, peer_ip: str, stats: dict) -> None:
        """Update detailed connection statistics for a peer."""
        username = self.online_users.get(peer_ip, peer_ip)
        stats_text = []
        
        if 'latency' in stats:
            latency = stats['latency']
            color = Qt.green if latency < 100 else Qt.yellow if latency < 500 else Qt.red
            stats_text.append(f"Latency: {latency}ms")
        
        if 'packet_loss' in stats:
            packet_loss = stats['packet_loss']
            if packet_loss > 0:
                stats_text.append(f"Packet Loss: {packet_loss:.1f}%")
        
        if 'bandwidth' in stats:
            bw = stats['bandwidth']
            stats_text.append(f"Bandwidth: {bw/1024:.1f} MB/s")
            
        # Update user list item with connection stats
        for i in range(self.user_list_widget.count()):
            item = self.user_list_widget.item(i)
            if peer_ip in item.text():
                base_text = item.text().split(" [")[0]
                item.setText(f"{base_text} [{' | '.join(stats_text)}]")
                if 'latency' in stats:
                    item.setForeground(color)
                break

    def show_network_error_gui(self, interface: str, error_msg: str, is_critical: bool) -> None:
        """Display network-specific errors."""
        title = "Critical Network Error" if is_critical else "Network Warning"
        message = f"Network interface {interface} encountered an issue:\n{error_msg}"
        
        if is_critical:
            QMessageBox.critical(self, title, message)
            self.statusBar().showMessage(f"Network Error on {interface}", 5000)
        else:
            QMessageBox.warning(self, title, message)
        
        self.append_to_chat_display(f"[Network Error] {interface}: {error_msg}")

    def show_transfer_error_gui(self, file_id: str, error_type: str, error_msg: str) -> None:
        """Display file transfer specific errors."""
        title = f"File Transfer Error: {error_type}"
        QMessageBox.warning(self, title, f"Transfer {file_id} failed:\n{error_msg}")
        self.file_transfer_area.append(f"[Error] Transfer {file_id}: {error_type} - {error_msg}")
        
        # Update any progress bars or status indicators
        progress_bar = self.findChild(QProgressBar, f"progress_{file_id}")
        if progress_bar:
            progress_bar.setFormat("Transfer Failed")
            progress_bar.setStyleSheet("QProgressBar::chunk { background-color: red; }")

    def update_peer_diagnostics_gui(self, peer_ip: str, diagnostic_info: dict) -> None:
        """Update comprehensive peer connection diagnostics."""
        username = self.online_users.get(peer_ip, peer_ip)
        
        # Create or update a detailed diagnostic display
        text = [f"Connection Diagnostics for {username} ({peer_ip}):"]
        
        if 'connection_uptime' in diagnostic_info:
            uptime = diagnostic_info['connection_uptime']
            text.append(f"Connection Uptime: {uptime:.1f} seconds")
            
        if 'avg_latency' in diagnostic_info:
            avg_latency = diagnostic_info['avg_latency']
            text.append(f"Average Latency: {avg_latency:.1f}ms")
            
        if 'packet_loss_rate' in diagnostic_info:
            loss_rate = diagnostic_info['packet_loss_rate']
            text.append(f"Packet Loss Rate: {loss_rate:.2f}%")
            
        if 'connection_quality' in diagnostic_info:
            quality = diagnostic_info['connection_quality']
            text.append(f"Connection Quality: {quality}")
            
        # Display in a dedicated diagnostic area or log
        if hasattr(self, 'diagnostic_area'):
            self.diagnostic_area.setText("\n".join(text))
        else:
            self.append_to_chat_display("\n".join(text))

    def update_network_metrics_gui(self, metrics: dict) -> None:
        """Update overall network performance metrics."""
        if not hasattr(self, 'network_metrics_label'):
            self.network_metrics_label = QLabel()
            self.statusBar().addPermanentWidget(self.network_metrics_label)
            
        # Format metrics into a concise display
        metrics_text = []
        
        if 'active_connections' in metrics:
            metrics_text.append(f"Connections: {metrics['active_connections']}")
            
        if 'total_bandwidth' in metrics:
            bw = metrics['total_bandwidth'] / 1024  # Convert to MB/s
            metrics_text.append(f"Bandwidth: {bw:.1f}MB/s")
            
        if 'avg_network_latency' in metrics:
            metrics_text.append(f"Avg Latency: {metrics['avg_network_latency']:.0f}ms")
            
        self.network_metrics_label.setText(" | ".join(metrics_text))

if __name__ == "__main__":
    app = QApplication(sys.argv)
    my_ip = get_local_ip()
    username, ok = QInputDialog.getText(None, "Username", "Enter your username:", text=os.getlogin() if hasattr(os, 'getlogin') else "User")
    if not ok or not username.strip(): username = f"User_{my_ip.split('.')[-1]}"
    
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path: sys.path.insert(0, project_root)

    main_win = LanMessengerApp(username=username, local_ip=my_ip)
    main_win.show()
    sys.exit(app.exec_())

