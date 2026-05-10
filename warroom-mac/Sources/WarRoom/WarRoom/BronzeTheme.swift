// BronzeTheme.swift — color palette + view modifiers tuned to the bronze
// steampunk gear app icon. Aged-watchmaker aesthetic: deep matte black
// background, warm bronze/copper/brass tones, ivory text, hairline copper
// dividers. The whole app is dark-mode locked because the icon's aesthetic
// is dark-mode-native.

import SwiftUI

extension Color {
    // Background — near-pure black, lifts a hair off true black so SwiftUI
    // surfaces still have material differentiation.
    static let bronzeBackground = Color(red: 0.039, green: 0.039, blue: 0.039)  // #0A0A0A
    static let bronzeSurface    = Color(red: 0.071, green: 0.063, blue: 0.055) // #121010 — sidebar, cards

    // Bronze family — gear teeth and primary accents
    static let bronzePrimary    = Color(red: 0.545, green: 0.353, blue: 0.169) // #8B5A2B  — gear body
    static let bronzeCopper     = Color(red: 0.722, green: 0.451, blue: 0.200) // #B87333  — lit edges, highlights
    static let bronzeBrass      = Color(red: 0.627, green: 0.471, blue: 0.251) // #A07840  — brass mid-tone
    static let bronzeDeep       = Color(red: 0.227, green: 0.141, blue: 0.063) // #3A2410  — iris shadow grooves
    static let bronzeLensBlack  = Color(red: 0.102, green: 0.055, blue: 0.031) // #1A0E08  — lens center

    // Text — aged ivory, no pure white anywhere
    static let bronzeIvory      = Color(red: 0.910, green: 0.847, blue: 0.706) // #E8D8B4
    static let bronzeIvoryDim   = Color(red: 0.600, green: 0.541, blue: 0.439) // #998A70
    static let bronzeIvoryFaint = Color(red: 0.380, green: 0.341, blue: 0.275) // #615746

    // Divider — same as iris-shadow color
    static let bronzeDivider    = Color(red: 0.227, green: 0.141, blue: 0.063) // #3A2410

    // Role accents — kept warm/family-coherent rather than blue/purple
    static let bronzeUser       = Color(red: 0.745, green: 0.529, blue: 0.235) // #BE873C — slightly cooler bronze
    static let bronzeAssistant  = Color(red: 0.722, green: 0.451, blue: 0.200) // #B87333 — copper
    static let bronzeTool       = Color(red: 0.510, green: 0.376, blue: 0.169) // #82602B — deeper aged
    static let bronzeProgress   = Color(red: 0.502, green: 0.408, blue: 0.275) // #806846 — subtle italic
    static let bronzeError      = Color(red: 0.659, green: 0.290, blue: 0.220) // #A84A38 — burnished red
}

// MARK: - Decorative background

/// Faint gear watermark in the bottom-right corner. Echoes the app icon
/// without competing for attention. ~3% opacity — present, not intrusive.
struct GearWatermark: View {
    var body: some View {
        GeometryReader { geo in
            ZStack {
                // Big gear in bottom-trailing — primary motif
                Image(systemName: "gearshape.2.fill")
                    .resizable()
                    .scaledToFit()
                    .frame(width: 360, height: 360)
                    .foregroundStyle(Color.bronzeDeep)
                    .opacity(0.045)
                    .rotationEffect(.degrees(15))
                    .position(x: geo.size.width - 100, y: geo.size.height - 60)

                // Small accent gear in top-leading — counterweight
                Image(systemName: "gearshape.fill")
                    .resizable()
                    .scaledToFit()
                    .frame(width: 140, height: 140)
                    .foregroundStyle(Color.bronzePrimary)
                    .opacity(0.035)
                    .rotationEffect(.degrees(-22))
                    .position(x: 80, y: 90)

                // Faint hairlines top + bottom — like the icon's measuring scale
                VStack {
                    Rectangle()
                        .fill(Color.bronzeDeep)
                        .frame(height: 0.5)
                        .opacity(0.5)
                    Spacer()
                    Rectangle()
                        .fill(Color.bronzeDeep)
                        .frame(height: 0.5)
                        .opacity(0.5)
                }
            }
            .allowsHitTesting(false)  // decoration must not eat clicks
        }
    }
}


// MARK: - View modifiers

/// Applies the bronze theme: dark scheme + black background + faint gear
/// watermark + bronze tint. Use on any top-level tab view.
struct BronzeBackgroundModifier: ViewModifier {
    func body(content: Content) -> some View {
        ZStack {
            Color.bronzeBackground.ignoresSafeArea()
            GearWatermark().ignoresSafeArea()
            content
        }
        .preferredColorScheme(.dark)
        .tint(Color.bronzeCopper)
    }
}

extension View {
    func bronzeTheme() -> some View {
        modifier(BronzeBackgroundModifier())
    }
}

/// Hairline divider in bronze. 0.5pt of #3A2410 — barely visible until you
/// look for it, like the score lines on the icon's measuring scale.
struct BronzeHairline: View {
    var body: some View {
        Rectangle()
            .fill(Color.bronzeDivider)
            .frame(height: 0.5)
    }
}
