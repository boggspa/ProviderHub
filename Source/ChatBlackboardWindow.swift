import AppKit
import SwiftUI

/// One Blackboard window per chat, showing the same SwiftUI views as the Team
/// pane. The window follows its own chat, not the main window's selection.
@MainActor
final class ChatBlackboardWindows: NSObject, NSWindowDelegate {
    static let shared = ChatBlackboardWindows()
    private var windows: [String: NSWindow] = [:]

    func show(model: ChatModel, chat: String) {
        let title = "Blackboard · " + (model.chats.first { $0.id == chat }?.title ?? "Chat")
        if let window = windows[chat] {
            window.title = title
            window.makeKeyAndOrderFront(nil); return
        }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 640),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = title
        window.isReleasedWhenClosed = false; window.delegate = self
        window.minSize = NSSize(width: 420, height: 360)
        window.contentView = NSHostingView(rootView: ChatBlackboardBoard(model: model, chat: chat))
        if let anchor = windows.values.first(where: \.isVisible) {
            window.setFrameTopLeftPoint(window.cascadeTopLeft(from: NSPoint(x: anchor.frame.minX, y: anchor.frame.maxY)))
        } else { window.center() }
        windows[chat] = window
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ notification: Notification) {
        guard let closing = notification.object as? NSWindow else { return }
        windows = windows.filter { $0.value !== closing }
    }
}
