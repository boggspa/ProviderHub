import AppKit
import AVFoundation
import ImageIO
import PDFKit
import SwiftUI
import UniformTypeIdentifiers

/// The Team Blackboard, drawn in the Team inspector pane and in its own window.
/// The worker owns the board (chat_blackboard.py); these views render its
/// latest snapshot and send user posts, removals and attachments back.
struct ChatBlackboardSection: View {
    @ObservedObject var model: ChatModel
    @ObservedObject var board: ChatBlackboardModel
    var chat: String
    init(model: ChatModel, chat: String) { self.model = model; board = model.blackboard; self.chat = chat }
    /// Message attachments appear on the board; a new one asks for a fresh snapshot.
    private var messageFiles: Int { model.entries.reduce(0) { $0 + ($1.kind == "user" ? $1.attachments?.count ?? 0 : 0) } }
    var body: some View {
        let state = board.state(chat)
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text("Blackboard").font(.system(size: 13, weight: .semibold))
                if let snapshot = state.snapshot {
                    Text("\(snapshot.posts.count)/\(snapshot.limits.posts)").font(.system(size: 10.5)).foregroundStyle(.secondary)
                        .help("Posts on this chat's board")
                }
                Spacer(minLength: 0)
                if state.request != nil { ProgressView().controlSize(.mini) }
                Button { ChatBlackboardWindows.shared.show(model: model, chat: chat) } label: {
                    Image(systemName: "arrow.up.left.and.arrow.down.right")
                }.buttonStyle(.plain).help("Open the Blackboard window").accessibilityLabel("Open Blackboard window")
            }
            Text("Pinned facts, decisions and files every member and you can refer to. Posts are reference notes, not verified facts.")
                .font(.system(size: 10.5)).foregroundStyle(.secondary)
            if let snapshot = state.snapshot {
                if snapshot.posts.isEmpty {
                    Text("No posts yet.").font(.system(size: 11.5)).foregroundStyle(.secondary)
                }
                ForEach(snapshot.latest) { post in ChatBlackboardPostCard(model: model, chat: chat, post: post) }
                if snapshot.posts.count > snapshot.latest.count {
                    Button("Show all \(snapshot.posts.count) posts") { ChatBlackboardWindows.shared.show(model: model, chat: chat) }
                        .buttonStyle(.link).font(.system(size: 11.5))
                }
                if !snapshot.attachments.isEmpty {
                    Text("Attachments").font(.system(size: 11.5, weight: .medium)).foregroundStyle(.secondary).padding(.top, 2)
                    ChatBlackboardAttachments(model: model, chat: chat, snapshot: snapshot, compact: true)
                }
            } else if state.notice.isEmpty {
                ProgressView().controlSize(.small)
            }
            ChatBlackboardComposer(model: model, chat: chat)
            if !state.notice.isEmpty {
                Text(state.notice).font(.system(size: 11.5)).foregroundStyle(.secondary).textSelection(.enabled)
            }
        }
        .padding(14)
        .dropDestination(for: URL.self) { urls, _ in model.attachToBlackboard(chat, urls: urls); return !urls.isEmpty }
        .task(id: chat + (model.connected ? "|on" : "|off")) { model.refreshBlackboard(chat) }
        .onChange(of: messageFiles) { _, _ in model.refreshBlackboard(chat) }
    }
}

/// Every post grouped by category, and the full attachments grid.
struct ChatBlackboardBoard: View {
    @ObservedObject var model: ChatModel
    @ObservedObject var board: ChatBlackboardModel
    var chat: String
    init(model: ChatModel, chat: String) { self.model = model; board = model.blackboard; self.chat = chat }
    var body: some View {
        let state = board.state(chat)
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "pin").foregroundStyle(.secondary)
                Text(model.chats.first { $0.id == chat }?.title ?? "Blackboard").font(.system(size: 14, weight: .semibold)).lineLimit(1)
                Spacer(minLength: 0)
                if let snapshot = state.snapshot {
                    Text("\(snapshot.posts.count)/\(snapshot.limits.posts) posts · " +
                         ByteCountFormatter.string(fromByteCount: Int64(snapshot.limits.usedBytes), countStyle: .file) + " of " +
                         ByteCountFormatter.string(fromByteCount: Int64(snapshot.limits.boardBytes), countStyle: .file))
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
                if state.request != nil { ProgressView().controlSize(.mini) }
                Button { model.refreshBlackboard(chat) } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.plain).help("Refresh").accessibilityLabel("Refresh Blackboard")
            }.padding(.horizontal, 18).padding(.vertical, 12)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let snapshot = state.snapshot {
                        if snapshot.posts.isEmpty {
                            Text("No posts yet. Team members pin facts, decisions and risks here; you can post below.")
                                .font(.system(size: 12)).foregroundStyle(.secondary)
                        }
                        ForEach(snapshot.groups, id: \.category) { group in
                            VStack(alignment: .leading, spacing: 8) {
                                Text(ChatBlackboardSnapshot.title(group.category) + " · \(group.posts.count)")
                                    .font(.system(size: 12, weight: .semibold)).foregroundStyle(ChatBlackboardPostCard.tint(group.category))
                                LazyVGrid(columns: [GridItem(.adaptive(minimum: 260), spacing: 10, alignment: .top)], alignment: .leading, spacing: 10) {
                                    ForEach(group.posts) { post in ChatBlackboardPostCard(model: model, chat: chat, post: post) }
                                }
                            }
                        }
                        VStack(alignment: .leading, spacing: 8) {
                            HStack {
                                Text("Attachments · \(snapshot.attachments.count)").font(.system(size: 12, weight: .semibold))
                                Spacer()
                                Button { model.chooseBlackboardFiles(chat) } label: { Label("Add files", systemImage: "paperclip") }.controlSize(.small)
                            }
                            if snapshot.attachments.isEmpty {
                                Text("Files attached to messages appear here automatically. Drop files or add links to pin more.")
                                    .font(.system(size: 11.5)).foregroundStyle(.secondary)
                            } else {
                                ChatBlackboardAttachments(model: model, chat: chat, snapshot: snapshot, compact: false)
                            }
                            if snapshot.omittedAttachments > 0 {
                                Text("\(snapshot.omittedAttachments) older message attachments are not listed.").font(.system(size: 10.5)).foregroundStyle(.secondary)
                            }
                        }
                    } else if state.notice.isEmpty {
                        ProgressView().controlSize(.small)
                    }
                    if !state.notice.isEmpty {
                        Text(state.notice).font(.system(size: 11.5)).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            ChatBlackboardComposer(model: model, chat: chat).padding(14)
        }
        .frame(minWidth: 420, minHeight: 360)
        .dropDestination(for: URL.self) { urls, _ in model.attachToBlackboard(chat, urls: urls); return !urls.isEmpty }
        .task(id: chat + (model.connected ? "|on" : "|off")) { model.refreshBlackboard(chat) }
    }
}

/// One post: author chip in the provider's accent, key, category, time and body.
struct ChatBlackboardPostCard: View {
    @ObservedObject var model: ChatModel
    var chat: String
    var post: ChatBlackboardPost
    @State private var expanded = false
    private typealias Semantic = HubTheme.Semantic
    private var accent: Color { post.byUser ? Semantic.ink : model.accent(for: post.route) }
    private var verbose: Bool { post.body.count > 160 || post.body.contains("\n") || (post.quote?.excerpt.count ?? 0) > 120 }
    static func tint(_ category: String) -> Color {
        switch category {
        case "decision": .blue
        case "fact": .green
        case "risk": HubTheme.Semantic.contextWarn
        case "do-not-repeat": HubTheme.Semantic.contextCritical
        default: HubTheme.Semantic.secondaryInk
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                HStack(spacing: 3) {
                    if post.byUser { Image(systemName: "person.fill").font(.system(size: 8)) }
                    else { ChatProviderIcon(presentation: model.models.first { $0.route == post.route }?.presentation, size: 10) }
                    Text(post.authorName).lineLimit(1)
                }
                .font(.system(size: 10.5, weight: .medium)).foregroundStyle(accent)
                .padding(.horizontal, 6).padding(.vertical, 2).background(Capsule().fill(accent.opacity(0.14)))
                Text(post.key).font(.system(size: 11, weight: .medium, design: .monospaced)).lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 4)
                Text(ChatBlackboardSnapshot.label(post.category)).font(.system(size: 9.5, weight: .medium))
                    .foregroundStyle(Self.tint(post.category))
                    .padding(.horizontal, 5).padding(.vertical, 1).background(Capsule().fill(Self.tint(post.category).opacity(0.13)))
            }
            Text(post.body).font(.system(size: 11.5)).foregroundStyle(Semantic.ink)
                .lineLimit(expanded ? nil : 3).fixedSize(horizontal: false, vertical: true)
            if let quote = post.quote {
                HStack(alignment: .top, spacing: 6) {
                    RoundedRectangle(cornerRadius: 1).fill(Semantic.hairline).frame(width: 2)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(quote.author + " · " + (quote.tool ?? quote.kind)).font(.system(size: 10)).foregroundStyle(.secondary)
                        Text(quote.excerpt).font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(expanded ? nil : 2)
                    }
                }.fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 4) {
                if let date = post.updatedDate {
                    Text(date.formatted(.relative(presentation: .named))).font(.system(size: 9.5)).foregroundStyle(.secondary)
                        .help(date.formatted(date: .abbreviated, time: .standard))
                }
                Spacer(minLength: 0)
                if verbose {
                    Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.system(size: 8.5)).foregroundStyle(.secondary)
                }
            }
        }
        .padding(8).frame(maxWidth: .infinity, alignment: .leading)
        .background(Semantic.raisedSurface, in: RoundedRectangle(cornerRadius: 7))
        .contentShape(Rectangle())
        .onTapGesture { if verbose { withAnimation(.easeOut(duration: 0.12)) { expanded.toggle() } } }
        .contextMenu {
            Button("Copy") {
                NSPasteboard.general.clearContents(); NSPasteboard.general.setString(post.key + ": " + post.body, forType: .string)
            }
            Button("Remove from Blackboard", role: .destructive) { model.removeBlackboardPost(chat, post: post.id) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityHint(verbose ? "Click to expand or collapse" : "")
    }
}

/// The user's post field. Keys are optional; the body seeds one when blank.
struct ChatBlackboardComposer: View {
    @ObservedObject var model: ChatModel
    @ObservedObject var board: ChatBlackboardModel
    var chat: String
    @State private var key = ""
    @State private var text = ""
    @State private var category = "note"
    @State private var link = ""
    @State private var linking = false
    @State private var pending: String?
    init(model: ChatModel, chat: String) { self.model = model; board = model.blackboard; self.chat = chat }
    private var bytes: Int { text.trimmingCharacters(in: .whitespacesAndNewlines).utf8.count }
    private var limit: Int { board.state(chat).snapshot?.limits.bodyBytes ?? 1500 }
    private var canPost: Bool {
        model.connected && board.state(chat).request == nil && bytes > 0 && bytes <= limit
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                TextField("key (optional)", text: $key).textFieldStyle(.roundedBorder)
                    .font(.system(size: 11.5, design: .monospaced))
                Picker("Category", selection: $category) {
                    ForEach(ChatBlackboardSnapshot.categories, id: \.self) { Text(ChatBlackboardSnapshot.label($0)).tag($0) }
                }.labelsHidden().pickerStyle(.menu).fixedSize()
            }
            TextField("Pin a fact, decision or risk for the Team…", text: $text, axis: .vertical)
                .textFieldStyle(.roundedBorder).font(.system(size: 12)).lineLimit(1...5)
                .onSubmit { post() }
            HStack(spacing: 8) {
                Button { model.chooseBlackboardFiles(chat) } label: { Image(systemName: "paperclip") }
                    .buttonStyle(.plain).help("Add files to the Blackboard").accessibilityLabel("Add files to Blackboard")
                Button { linking.toggle() } label: { Image(systemName: "link") }
                    .buttonStyle(.plain).help("Add a link to the Blackboard").accessibilityLabel("Add link to Blackboard")
                Spacer(minLength: 0)
                if bytes > limit * 4 / 5 {
                    Text("\(bytes)/\(limit)").font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(bytes > limit ? HubTheme.Semantic.contextCritical : HubTheme.Semantic.secondaryInk)
                }
                Button("Post") { post() }.controlSize(.small).disabled(!canPost)
                    .keyboardShortcut(.return, modifiers: .command)
            }
            if linking {
                HStack(spacing: 6) {
                    TextField("https://…", text: $link).textFieldStyle(.roundedBorder).font(.system(size: 11.5)).onSubmit { addLink() }
                    Button("Add") { addLink() }.controlSize(.small).disabled(link.trimmingCharacters(in: .whitespaces).isEmpty || !model.connected)
                }
            }
        }
        .onChange(of: board.state(chat).settled) { _, settled in
            guard let settled, settled == pending else { return }
            pending = nil
            guard board.state(chat).settledOK else { return }
            if linking && !link.isEmpty && text.isEmpty { link = ""; linking = false } else { key = ""; text = "" }
        }
    }
    private func post() {
        guard canPost else { return }
        pending = model.postToBlackboard(chat, key: key, body: text, category: category)
    }
    private func addLink() {
        pending = model.linkToBlackboard(chat, url: link)
    }
}

/// Board files and links: a compact strip in the inspector, a grid in the window.
struct ChatBlackboardAttachments: View {
    @ObservedObject var model: ChatModel
    var chat: String
    var snapshot: ChatBlackboardSnapshot
    var compact: Bool
    var body: some View {
        if compact {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: 8) {
                    ForEach(snapshot.attachments.prefix(24)) { file in
                        ChatBlackboardTile(model: model, chat: chat, file: file, directory: snapshot.thumbnails, compact: true)
                    }
                }.padding(.vertical, 2)
            }
        } else {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 12, alignment: .top)], alignment: .leading, spacing: 12) {
                ForEach(snapshot.attachments) { file in
                    ChatBlackboardTile(model: model, chat: chat, file: file, directory: snapshot.thumbnails, compact: false)
                }
            }
        }
    }
}

private struct ChatBlackboardTile: View {
    @ObservedObject var model: ChatModel
    var chat: String
    var file: ChatBlackboardFile
    var directory: String
    var compact: Bool
    @State private var image: NSImage?
    private typealias Semantic = HubTheme.Semantic
    private var width: CGFloat { compact ? 92 : 150 }
    private var height: CGFloat { compact ? 54 : 92 }
    private var glyph: String {
        switch file.kind {
        case "url": "link"; case "video": "film"; case "audio": "waveform"; case "image": "photo"
        case "pdf": "doc.richtext"; default: ChatAttachmentStrip.glyph(for: file.name)
        }
    }
    private var detail: String {
        if file.kind == "url" { return URL(string: file.url ?? "")?.host ?? "Link" }
        return ChatAttachmentStrip.size(file.size) + (file.source == "message" ? " · message" : "")
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Button(action: open) { preview }.buttonStyle(.plain).help(file.url ?? file.name)
            Text(file.name).font(.system(size: 10.5)).foregroundStyle(Semantic.ink).lineLimit(1).truncationMode(.middle)
            Text(detail).font(.system(size: 9.5)).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
        }
        .frame(width: width, alignment: .leading)
        .contextMenu {
            Button("Open") { open() }
            if let path = file.path {
                Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)]) }
            }
            if let url = file.url {
                Button("Copy Link") { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(url, forType: .string) }
            }
            if file.removable {
                Button("Remove from Blackboard", role: .destructive) { model.detachFromBlackboard(chat, attachment: file.id) }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Blackboard \(file.kind): \(file.name)")
        .task(id: file.id) { image = await ChatBlackboardThumbnails.load(file, directory: directory) }
    }
    @ViewBuilder private var preview: some View {
        ZStack {
            RoundedRectangle(cornerRadius: HubTheme.Radius.row).fill(Semantic.raisedSurface)
            if let image {
                if file.kind == "audio" {
                    Image(nsImage: image).renderingMode(.template).resizable().foregroundStyle(Semantic.secondaryInk).padding(.horizontal, 6)
                } else {
                    Image(nsImage: image).resizable().aspectRatio(contentMode: file.kind == "pdf" ? .fit : .fill)
                }
            } else {
                Image(systemName: glyph).font(.system(size: compact ? 16 : 22)).foregroundStyle(Semantic.secondaryInk)
            }
            if file.kind == "video" {
                Image(systemName: "play.circle.fill").font(.system(size: compact ? 14 : 20)).symbolRenderingMode(.palette)
                    .foregroundStyle(.white, Color.black.opacity(0.5))
            }
        }
        .frame(width: width, height: height)
        .clipShape(RoundedRectangle(cornerRadius: HubTheme.Radius.row))
        .overlay(RoundedRectangle(cornerRadius: HubTheme.Radius.row).stroke(Semantic.hairline, lineWidth: 1))
    }
    private func open() {
        if let link = file.url, let url = URL(string: link), ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
            NSWorkspace.shared.open(url)
        } else if let path = file.path {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        }
    }
}

/// Small previews for board files, rendered off the main thread and cached as
/// PNGs (at most `maxEdge` pixels on a side) in the chat's private
/// `blackboard/thumbnails` folder. Images use ImageIO's thumbnail decoder,
/// video a filmstrip of evenly spaced frames, audio a peak waveform read with
/// AVAssetReader, and PDFs their first page. Other files keep a glyph chip.
enum ChatBlackboardThumbnails {
    static let maxEdge = 360
    static let waveformBuckets = 200
    static let filmstripFrames = 5
    private static let memory: NSCache<NSString, NSImage> = { let cache = NSCache<NSString, NSImage>(); cache.countLimit = 200; return cache }()

    static func load(_ file: ChatBlackboardFile, directory: String) async -> NSImage? {
        guard let path = file.path, ["image", "video", "audio", "pdf"].contains(file.kind), !directory.isEmpty,
              file.id.allSatisfy({ $0.isLetter || $0.isNumber }) else { return nil }
        let key = (directory + "/" + file.id) as NSString
        if let image = memory.object(forKey: key) { return image }
        let kind = file.kind, id = file.id
        let image = await Task.detached(priority: .utility) { () -> NSImage? in
            let folder = URL(fileURLWithPath: directory, isDirectory: true)
            let target = folder.appendingPathComponent(id + ".png")
            if let data = try? Data(contentsOf: target), let image = NSImage(data: data) { return image }
            guard let rendered = await render(kind, URL(fileURLWithPath: path)) else { return nil }
            store(rendered, at: target, in: folder)
            return NSImage(cgImage: rendered, size: NSSize(width: rendered.width / 2, height: rendered.height / 2))
        }.value
        if let image { memory.setObject(image, forKey: key) }
        return image
    }

    static func render(_ kind: String, _ url: URL) async -> CGImage? {
        switch kind {
        case "image": return still(url)
        case "pdf": return page(url)
        case "video": return await filmstrip(url)
        case "audio": return await waveform(url)
        default: return nil
        }
    }

    static func still(_ url: URL) -> CGImage? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        return CGImageSourceCreateThumbnailAtIndex(source, 0, [
            kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maxEdge] as CFDictionary)
    }

    static func page(_ url: URL) -> CGImage? {
        guard let page = PDFDocument(url: url)?.page(at: 0) else { return nil }
        let bounds = page.bounds(for: .cropBox)
        guard bounds.width > 0, bounds.height > 0 else { return nil }
        let scale = CGFloat(maxEdge) / max(bounds.width, bounds.height)
        let size = CGSize(width: max(1, (bounds.width * scale).rounded()), height: max(1, (bounds.height * scale).rounded()))
        guard let context = canvas(size) else { return nil }
        context.setFillColor(NSColor.white.cgColor); context.fill(CGRect(origin: .zero, size: size))
        context.scaleBy(x: scale, y: scale); context.translateBy(x: -bounds.minX, y: -bounds.minY)
        page.draw(with: .cropBox, to: context)
        return context.makeImage()
    }

    static func filmstrip(_ url: URL) async -> CGImage? {
        let asset = AVURLAsset(url: url)
        guard let duration = try? await asset.load(.duration), duration.seconds.isFinite, duration.seconds > 0 else { return nil }
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 160, height: 160)
        let tolerance = CMTime(seconds: max(0.1, duration.seconds / Double(filmstripFrames * 4)), preferredTimescale: 600)
        generator.requestedTimeToleranceBefore = tolerance; generator.requestedTimeToleranceAfter = tolerance
        var frames: [CGImage] = []
        for index in 0..<filmstripFrames {
            if Task.isCancelled { return nil }
            let time = CMTime(seconds: duration.seconds * (Double(index) + 0.5) / Double(filmstripFrames), preferredTimescale: 600)
            if let frame = try? await generator.image(at: time).image { frames.append(frame) }
        }
        guard let first = frames.first, first.height > 0 else { return nil }
        let height = CGFloat(min(maxEdge / filmstripFrames * 3 / 2, first.height))
        let widths = frames.map { CGFloat($0.width) * height / CGFloat(max(1, $0.height)) }
        let gap: CGFloat = 2
        var total = widths.reduce(0, +) + gap * CGFloat(frames.count - 1)
        let shrink = min(1, CGFloat(maxEdge) / total)
        total = (total * shrink).rounded()
        guard let context = canvas(CGSize(width: max(1, total), height: max(1, (height * shrink).rounded()))) else { return nil }
        var x: CGFloat = 0
        for (frame, width) in zip(frames, widths) {
            context.draw(frame, in: CGRect(x: x, y: 0, width: width * shrink, height: height * shrink))
            x += (width + gap) * shrink
        }
        return context.makeImage()
    }

    /// Peak amplitude per bucket from 16-bit PCM. Peaks are first gathered per
    /// fixed run of samples (so the duration need not be known), then reduced
    /// to `waveformBuckets` bars.
    static func waveform(_ url: URL) async -> CGImage? {
        let asset = AVURLAsset(url: url)
        guard let track = try? await asset.loadTracks(withMediaType: .audio).first,
              let reader = try? AVAssetReader(asset: asset) else { return nil }
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
            AVFormatIDKey: kAudioFormatLinearPCM, AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsNonInterleaved: false])
        output.alwaysCopiesSampleData = false
        guard reader.canAdd(output) else { return nil }
        reader.add(output)
        guard reader.startReading() else { return nil }
        let run = 2048
        var peaks: [Float] = [], peak: Int32 = 0, count = 0
        var samples: [Int16] = []
        while let buffer = output.copyNextSampleBuffer() {
            if Task.isCancelled || peaks.count > 4_000_000 { reader.cancelReading(); return nil }
            guard let block = CMSampleBufferGetDataBuffer(buffer) else { continue }
            let length = CMBlockBufferGetDataLength(block) / 2
            if samples.count < length { samples = [Int16](repeating: 0, count: length) }
            let copied = samples.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length * 2, destination: $0.baseAddress!) }
            guard copied == kCMBlockBufferNoErr else { continue }
            for index in 0..<length {
                let value = abs(Int32(samples[index]))
                if value > peak { peak = value }
                count += 1
                if count == run { peaks.append(Float(peak) / 32768); peak = 0; count = 0 }
            }
        }
        if count > 0 { peaks.append(Float(peak) / 32768) }
        guard reader.status == .completed, !peaks.isEmpty else { return nil }
        let buckets = waveformBuckets
        var bars = [Float](repeating: 0, count: buckets)
        for bucket in 0..<buckets {
            let start = bucket * peaks.count / buckets, end = max(start + 1, (bucket + 1) * peaks.count / buckets)
            bars[bucket] = peaks[min(start, peaks.count - 1)..<min(end, peaks.count)].max() ?? 0
        }
        let loudest = max(bars.max() ?? 0, 0.001)
        let size = CGSize(width: buckets * 2, height: 72)
        guard let context = canvas(size) else { return nil }
        context.setFillColor(NSColor.black.cgColor)
        for (index, bar) in bars.enumerated() {
            let height = max(1.5, CGFloat(bar / loudest) * (size.height - 4))
            context.fill(CGRect(x: CGFloat(index * 2), y: (size.height - height) / 2, width: 1.4, height: height))
        }
        return context.makeImage()
    }

    private static func canvas(_ size: CGSize) -> CGContext? {
        CGContext(data: nil, width: Int(size.width), height: Int(size.height), bitsPerComponent: 8, bytesPerRow: 0,
                  space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
    }

    private static func store(_ image: CGImage, at target: URL, in folder: URL) {
        // Private like the rest of the chat's storage; a cache that cannot be
        // written only costs a re-render next time.
        guard (try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                                         attributes: [.posixPermissions: 0o700])) != nil else { return }
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, UTType.png.identifier as CFString, 1, nil) else { return }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { return }
        try? (data as Data).write(to: target, options: [.atomic])
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: target.path)
    }
}
