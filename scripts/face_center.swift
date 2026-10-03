// Статичний центр кропу ведучого (MVP): Apple Vision шукає обличчя
// на кількох кадрах; результат — медіана рамок обличчя.
// Виклик: swift face_center.swift <video> [samples]
// Друкує JSON: {"found": true, "x":…, "y":…, "w":…, "h":…, "frames": N} (0..1, origin top-left)
// або {"found": false, "frames": N}.

import AVFoundation
import Foundation
import Vision

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: face_center.swift <video> [samples]\n".data(using: .utf8)!)
    exit(2)
}
let url = URL(fileURLWithPath: args[1])
let samples = args.count > 2 ? Int(args[2]) ?? 12 : 12

let asset = AVURLAsset(url: url)
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.requestedTimeToleranceBefore = .zero
generator.requestedTimeToleranceAfter = .zero

let semaphore = DispatchSemaphore(value: 0)
var duration = 0.0
Task {
    duration = (try? await asset.load(.duration).seconds) ?? 0
    semaphore.signal()
}
semaphore.wait()

var boxes: [CGRect] = []
for i in 0..<samples {
    let t = duration * (Double(i) + 0.5) / Double(samples)
    guard let image = try? generator.copyCGImage(at: CMTime(seconds: t, preferredTimescale: 600), actualTime: nil) else {
        continue
    }
    let request = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try? handler.perform([request])
    // найбільше обличчя в кадрі
    if let face = (request.results ?? []).max(by: { $0.boundingBox.width < $1.boundingBox.width }) {
        let b = face.boundingBox  // origin bottom-left
        boxes.append(CGRect(x: b.minX, y: 1 - b.maxY, width: b.width, height: b.height))
    }
}

func median(_ v: [Double]) -> Double {
    let s = v.sorted()
    return s.isEmpty ? 0 : s[s.count / 2]
}

if boxes.count * 3 < samples {  // обличчя менш ніж на третині кадрів — ненадійно
    print("{\"found\": false, \"frames\": \(boxes.count)}")
} else {
    let x = median(boxes.map { Double($0.minX) })
    let y = median(boxes.map { Double($0.minY) })
    let w = median(boxes.map { Double($0.width) })
    let h = median(boxes.map { Double($0.height) })
    print(String(format: "{\"found\": true, \"x\": %.4f, \"y\": %.4f, \"w\": %.4f, \"h\": %.4f, \"frames\": %d}",
                 x, y, w, h, boxes.count))
}
