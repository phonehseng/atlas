// Marquee liquid glass: frosts and refracts the video under the player controls.
// The controls (osc/marquee.lua) push up to four rounded rectangles as runtime parameters, in pixels of the
// video area on screen: gNx/gNy/gNw/gNh (box), gNr (corner radius), gNa (strength 0..1, follows the controls'
// fade). g_vw/g_vh is the video's size on screen: the hooked image may be another size (before/after scaling),
// so all geometry is done in screen pixels and only converted for sampling.
// The crisp rim, highlight and icons are drawn by the OSC on top (OSD is composited after this hook).

//!PARAM g_on
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 1.0
0.0

//!PARAM g_blur
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 64.0
12.0

//!PARAM g_vw
//!TYPE DYNAMIC float
//!MINIMUM 1.0
//!MAXIMUM 100000.0
1920.0

//!PARAM g_vh
//!TYPE DYNAMIC float
//!MINIMUM 1.0
//!MAXIMUM 100000.0
1080.0

//!PARAM g0x
//!TYPE DYNAMIC float
//!MINIMUM -100000.0
//!MAXIMUM 100000.0
0.0

//!PARAM g0y
//!TYPE DYNAMIC float
//!MINIMUM -100000.0
//!MAXIMUM 100000.0
0.0

//!PARAM g0w
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 100000.0
0.0

//!PARAM g0h
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 100000.0
0.0

//!PARAM g0r
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 10000.0
0.0

//!PARAM g0a
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 1.0
0.0

//!PARAM g1x
//!TYPE DYNAMIC float
//!MINIMUM -100000.0
//!MAXIMUM 100000.0
0.0

//!PARAM g1y
//!TYPE DYNAMIC float
//!MINIMUM -100000.0
//!MAXIMUM 100000.0
0.0

//!PARAM g1w
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 100000.0
0.0

//!PARAM g1h
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 100000.0
0.0

//!PARAM g1r
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 10000.0
0.0

//!PARAM g1a
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 1.0
0.0

//!PARAM g2x
//!TYPE DYNAMIC float
//!MINIMUM -100000.0
//!MAXIMUM 100000.0
0.0

//!PARAM g2y
//!TYPE DYNAMIC float
//!MINIMUM -100000.0
//!MAXIMUM 100000.0
0.0

//!PARAM g2w
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 100000.0
0.0

//!PARAM g2h
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 100000.0
0.0

//!PARAM g2r
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 10000.0
0.0

//!PARAM g2a
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 1.0
0.0

//!PARAM g3x
//!TYPE DYNAMIC float
//!MINIMUM -100000.0
//!MAXIMUM 100000.0
0.0

//!PARAM g3y
//!TYPE DYNAMIC float
//!MINIMUM -100000.0
//!MAXIMUM 100000.0
0.0

//!PARAM g3w
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 100000.0
0.0

//!PARAM g3h
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 100000.0
0.0

//!PARAM g3r
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 10000.0
0.0

//!PARAM g3a
//!TYPE DYNAMIC float
//!MINIMUM 0.0
//!MAXIMUM 1.0
0.0

//!HOOK OUTPUT
//!BIND HOOKED
//!DESC Marquee liquid glass

#define TAPS 24

vec4 hook() {
    vec4 color = HOOKED_texOff(0);
    if (g_on < 0.5)
        return color;
    vec2 view = vec2(g_vw, g_vh);
    vec2 px = HOOKED_pos * view;
    vec4 R[4] = vec4[4](
        vec4(g0x, g0y, g0w, g0h),
        vec4(g1x, g1y, g1w, g1h),
        vec4(g2x, g2y, g2w, g2h),
        vec4(g3x, g3y, g3w, g3h));
    vec2 RA[4] = vec2[4](
        vec2(g0r, g0a),
        vec2(g1r, g1a),
        vec2(g2r, g2a),
        vec2(g3r, g3a));

    // the (first) glass rectangle this pixel is inside
    int hit = -1;
    float dist = 0.0;
    vec2 normal = vec2(0.0);
    for (int i = 0; i < 4; i++) {
        if (RA[i].y <= 0.001 || R[i].z <= 0.0)
            continue;
        vec2 hs = R[i].zw * 0.5;
        vec2 dp = px - (R[i].xy + hs);
        float r = min(RA[i].x, min(hs.x, hs.y));
        vec2 q = abs(dp) - hs + r;
        float d = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
        if (d < 0.0) {
            hit = i;
            dist = -d;  // distance to the edge, inside
            vec2 n = (q.x > 0.0 && q.y > 0.0) ? normalize(q) : (q.x > q.y ? vec2(1.0, 0.0) : vec2(0.0, 1.0));
            normal = n * sign(dp);
            break;
        }
    }
    if (hit < 0)
        return color;

    float strength = RA[hit].y;
    vec2 size = R[hit].zw;
    float rim = min(min(size.x, size.y) * 0.5, 28.0);

    // lens: near the rim the glass bends light, pulling in the picture from just outside the edge
    float e = clamp(1.0 - dist / rim, 0.0, 1.0);
    vec2 src = px + normal * (e * e * e) * rim * 0.9;
    // a gentle magnification towards the middle
    vec2 centre = R[hit].xy + size * 0.5;
    src = centre + (src - centre) * 0.985;

    // frost: golden-angle disk blur around the refracted point
    float radius = g_blur * (0.65 + 0.35 * strength);
    vec3 acc = vec3(0.0);
    for (int i = 0; i < TAPS; i++) {
        float fi = float(i);
        float rr = sqrt((fi + 0.5) / float(TAPS)) * radius;
        float th = fi * 2.39996323;
        vec2 o = vec2(cos(th), sin(th)) * rr;
        acc += HOOKED_tex((src + o) / view).rgb;
    }
    vec3 glass = acc / float(TAPS);

    // a touch more saturation and light, then the Clear Home navy tint
    float luma = dot(glass, vec3(0.2126, 0.7152, 0.0722));
    glass = mix(vec3(luma), glass, 1.18);
    glass = glass * 0.8 + 0.02;
    glass = mix(glass, vec3(0.078, 0.125, 0.247), 0.34);
    // Clear Home's 140deg sheen: a little lighter towards the top left
    vec2 rel = (px - R[hit].xy) / size;
    glass += vec3(0.05) * clamp(1.0 - (rel.x * 0.35 + rel.y * 0.65), 0.0, 1.0);
    // brighter just inside the rim (light caught by the bent edge)
    glass += vec3(0.06) * e * e;

    float edge = smoothstep(0.0, 1.5, dist);  // anti-aliased boundary
    color.rgb = mix(color.rgb, glass, strength * edge);
    return color;
}
