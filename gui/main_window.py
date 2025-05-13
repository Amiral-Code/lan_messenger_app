import sys
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QTextEdit, QListWidget, QPushButton, QLabel, QSplitter, QFrame, QStatusBar, QAction
)
from PyQt5.QtCore import Qt

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LAN Messenger and File Sharer")
        self.setGeometry(100, 100, 900, 700)  # x, y, width, height

        self._setup_main_layout()
        self._setup_menu_bar()

    def _setup_main_layout(self):
        """Setup the main layout of the application."""
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QHBoxLayout(main_widget)

        # Left panel: User list and File Transfer area
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_panel.setFixedWidth(250)

        user_list_label = QLabel("Users Online")
        self.user_list_widget = QListWidget()
        left_layout.addWidget(user_list_label)
        left_layout.addWidget(self.user_list_widget)

        file_transfer_label = QLabel("File Transfers")
        self.file_transfer_area = QTextEdit()
        self.file_transfer_area.setReadOnly(True)
        self.file_transfer_area.setPlaceholderText("File transfer status...")
        left_layout.addWidget(file_transfer_label)
        left_layout.addWidget(self.file_transfer_area)

        # Right panel: Chat area
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        self.chat_display_area = QTextEdit()
        self.chat_display_area.setReadOnly(True)
        self.chat_display_area.setPlaceholderText("Messages will appear here...")
        right_layout.addWidget(self.chat_display_area)

        # Typing indicator label (will be part of status bar or near input)
        self.typing_indicator_label = QLabel("") # Initially empty
        self.typing_indicator_label.setFixedHeight(20)
        right_layout.addWidget(self.typing_indicator_label)

        message_input_layout = QHBoxLayout()
        self.message_input_field = QTextEdit()
        self.message_input_field.setFixedHeight(60) 
        self.message_input_field.setPlaceholderText("Type your message here...")
        # self.message_input_field.textChanged.connect(self.handle_text_changed) # For typing indicator
        
        self.send_message_button = QPushButton("Send")
        # self.send_message_button.clicked.connect(self.send_message_action) # Connected in main_app.py

        self.attach_file_button = QPushButton("Attach File")
        # self.attach_file_button.clicked.connect(self.attach_file_action) # Connected in main_app.py

        message_input_layout.addWidget(self.message_input_field)
        message_input_layout.addWidget(self.send_message_button)
        message_input_layout.addWidget(self.attach_file_button)
        right_layout.addLayout(message_input_layout)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([250, 650])
        main_layout.addWidget(splitter)

        self.create_status_bar() # Add status bar

    def _setup_menu_bar(self):
        """Setup the menu bar with actions."""
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")

        settings_action = QAction("&Settings", self)
        settings_action.setObjectName("settings_action") # For connecting in main_app
        file_menu.addAction(settings_action)
        
        exit_action = QAction("&Exit", self)
        exit_action.setObjectName("exit_action")
        exit_action.triggered.connect(self.close) # Default close action
        file_menu.addAction(exit_action)

        help_menu = menubar.addMenu("&Help")
        about_action = QAction("&About", self)
        about_action.setObjectName("about_action")
        help_menu.addAction(about_action)

    def create_status_bar(self):
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.statusBar.showMessage("Ready")

    # Placeholder methods that will be overridden or connected in LanMessengerApp
    def send_message_action(self):
        # This method will be implemented in the main application logic (LanMessengerApp)
        # For now, it can be a placeholder or connected there.
        print("Send message action triggered in BaseMainWindow - should be handled by LanMessengerApp")
        pass

    def attach_file_action(self):
        # This method will be implemented in the main application logic (LanMessengerApp)
        print("Attach file action triggered in BaseMainWindow - should be handled by LanMessengerApp")
        pass

    # def handle_text_changed(self):
    #     # This method will be connected and implemented in LanMessengerApp for typing status
    #     print("Text changed - for typing indicator")
    #     pass

if __name__ == "__main__":
    app = QApplication(sys.argv)
    # For testing the base window standalone
    # In the actual app, LanMessengerApp(BaseMainWindow) will be instantiated.
    main_win = MainWindow() 
    main_win.show()
    sys.exit(app.exec_())

