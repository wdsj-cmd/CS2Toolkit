import sys
import os
import traceback
from PySide6.QtWidgets import QApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket

try:
    from ctypes import windll
    myappid = 'Moon4Quartz.cs2.toolkit'
    windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
except ImportError:
    pass

SERVER_NAME = "CS2ToolkitSingleInstanceServer_v1"


def log_unhandled_exception(kind, error, tb):
    """Keep Python/Qt callback errors visible in the windowed executable."""
    try:
        directory = os.path.join(os.environ.get('LOCALAPPDATA', os.getcwd()), 'CS2Toolkit')
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, 'flashfix-errors.log'), 'a', encoding='utf-8') as log:
            traceback.print_exception(kind, error, tb, file=log)
    except OSError:
        pass
    if sys.stderr is not None:
        sys.__excepthook__(kind, error, tb)

if __name__ == "__main__":
    sys.excepthook = log_unhandled_exception
    app = QApplication(sys.argv)
    
    # 检查是否已有实例运行
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    if socket.waitForConnected(500):
        socket.write(b"WAKE_UP")
        socket.waitForBytesWritten(500)
        sys.exit(0)
        
    # 如果没有，则创建本地服务器监听
    server = QLocalServer()
    server.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)
    
    from app.main_window import CS2Tool
    window = CS2Tool()
    
    def on_new_connection():
        client = server.nextPendingConnection()
        client.waitForReadyRead(500)
        msg = client.readAll().data()
        if msg == b"WAKE_UP":
            if window.isHidden() or window.isMinimized():
                window.show_window()
            else:
                window.activateWindow()
                window.raise_()
        client.disconnectFromServer()
        
    server.newConnection.connect(on_new_connection)
    
    window.show()
    sys.exit(app.exec())
