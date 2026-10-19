shader_type spatial;
render_mode blend_mix, depth_draw_opaque, cull_back, diffuse_burley, specular_schlick_ggx;

// R terrain id, G road type, B river type (values 0..255)
uniform sampler2D cell_tex : filter_nearest, repeat_disable;
// fine path field (8 texels/cell): R road distance, G river distance (x0.7 cells), B road type, A river type
uniform sampler2D path_tex : filter_linear, repeat_disable;
// fog of war incl. border (1 = explored), linear filtered for soft edges
uniform sampler2D fog_tex : filter_linear, repeat_disable;
uniform sampler2D noise_tex : filter_linear_mipmap, repeat_enable;
uniform sampler2D noise_nrm : filter_linear_mipmap, repeat_enable;
uniform sampler2D crack_tex : filter_linear_mipmap, repeat_enable;
uniform sampler2DArray tex_array : source_color, filter_linear_mipmap_anisotropic, repeat_enable;
uniform int tex_mask = 0;
uniform float map_size = 48.0;
uniform float border = 10.0;
uniform int level = 0;
uniform vec2 hover_cell = vec2(-100.0);
uniform bool show_grid = false;

const vec2 DIRS[8] = {vec2(1.0, 0.0), vec2(1.0, 1.0), vec2(0.0, 1.0), vec2(-1.0, 1.0), vec2(-1.0, 0.0),
	vec2(-1.0, -1.0), vec2(0.0, -1.0), vec2(1.0, -1.0)};

varying vec3 wpos;
varying vec3 wnrm;

vec3 srgb(vec3 c) { return pow(c, vec3(2.2)); }

// explored value with a soft fade over a few cells beyond the map edge
float fog_at(sampler2D ft, vec2 p, float msize, float brd) {
	vec2 pc = clamp(p, vec2(0.5), vec2(msize - 0.5));
	float f = texture(ft, (pc + brd) / (msize + 2.0 * brd)).r;
	vec2 o = max(max(-p, p - msize), vec2(0.0));
	float outside = length(o);
	return f * (1.0 - smoothstep(0.0, 3.5, outside));
}

void vertex() {
	// unexplored ground is pressed flat under the fog clouds (cliffs must not poke through)
	vec2 vp = clamp(VERTEX.xz, vec2(0.5), vec2(map_size - 0.5));
	float fv = textureLod(fog_tex, (vp + border) / (map_size + 2.0 * border), 0.0).r;
	vec2 vo = max(max(-VERTEX.xz, VERTEX.xz - map_size), vec2(0.0));
	fv *= 1.0 - smoothstep(0.0, 3.5, length(vo));
	VERTEX.y = mix(min(VERTEX.y, 0.2), VERTEX.y, smoothstep(0.05, 0.5, fv));
	wpos = (MODEL_MATRIX * vec4(VERTEX, 1.0)).xyz;
	wnrm = normalize((MODEL_MATRIX * vec4(NORMAL, 0.0)).xyz);
}

int cell_type(ivec2 c) {
	ivec2 cc = clamp(c, ivec2(0), ivec2(int(map_size) - 1));
	return int(texelFetch(cell_tex, cc, 0).r * 255.0 + 0.5);
}

float seg_dist(vec2 p, vec2 a, vec2 b) {
	vec2 ab = b - a;
	float t = clamp(dot(p - a, ab) / max(dot(ab, ab), 1e-6), 0.0, 1.0);
	return length(p - (a + ab * t));
}

// nz: x macro, y mid, z fine, w micro (0..1); cr: cellular (0..1)
void terrain_mat(int t, vec2 p, vec4 nz, float cr, out vec3 alb, out float rough, out vec3 emi, out float bump) {
	emi = vec3(0.0);
	rough = 0.9;
	bump = 0.5;
	if (t == 3) {            // snow
		alb = mix(srgb(vec3(0.74, 0.79, 0.88)), srgb(vec3(0.93, 0.95, 0.98)), smoothstep(0.3, 0.7, nz.x * 0.5 + nz.y * 0.5));
		alb *= 0.95 + 0.05 * nz.z;
		rough = 0.65;
		bump = 0.35;
	} else if (t == 2) {     // grass
		vec3 a = srgb(vec3(0.27, 0.40, 0.13));
		vec3 b = srgb(vec3(0.40, 0.48, 0.17));
		vec3 c = srgb(vec3(0.18, 0.30, 0.10));
		alb = mix(mix(a, b, smoothstep(0.35, 0.7, nz.x)), c, smoothstep(0.55, 0.8, nz.y) * 0.7);
		alb *= 0.85 + 0.3 * nz.w;
		bump = 0.6;
	} else if (t == 0) {     // dirt
		alb = mix(srgb(vec3(0.40, 0.29, 0.19)), srgb(vec3(0.50, 0.38, 0.25)), smoothstep(0.3, 0.7, nz.y));
		alb = mix(alb, srgb(vec3(0.58, 0.52, 0.45)), smoothstep(0.08, 0.0, cr) * 0.6);
		alb *= 0.88 + 0.24 * nz.w;
		bump = 0.7;
	} else if (t == 1) {     // sand
		alb = mix(srgb(vec3(0.72, 0.62, 0.43)), srgb(vec3(0.82, 0.73, 0.52)), smoothstep(0.3, 0.7, nz.y));
		alb *= 0.93 + 0.1 * sin(p.x * 9.0 + nz.y * 8.0);
		bump = 0.3;
	} else if (t == 4) {     // swamp
		alb = mix(srgb(vec3(0.20, 0.24, 0.12)), srgb(vec3(0.30, 0.32, 0.16)), nz.z);
		float puddle = smoothstep(0.56, 0.62, nz.y);
		alb = mix(alb, srgb(vec3(0.07, 0.10, 0.09)), puddle);
		rough = mix(0.9, 0.08, puddle);
		bump = mix(0.6, 0.0, puddle);
	} else if (t == 5) {     // rough
		alb = mix(srgb(vec3(0.42, 0.38, 0.31)), srgb(vec3(0.52, 0.47, 0.39)), smoothstep(0.3, 0.7, nz.y));
		float stones = smoothstep(0.16, 0.05, cr);
		alb = mix(alb, srgb(vec3(0.60, 0.58, 0.55)) * (0.8 + 0.4 * nz.w), stones);
		bump = 0.9;
	} else if (t == 6) {     // subterranean
		alb = mix(srgb(vec3(0.27, 0.21, 0.17)), srgb(vec3(0.36, 0.29, 0.22)), smoothstep(0.3, 0.7, nz.y));
		alb *= mix(0.65, 1.0, smoothstep(0.0, 0.07, cr));
		bump = 0.8;
	} else if (t == 7) {     // lava
		alb = srgb(vec3(0.12, 0.10, 0.09)) * (0.8 + 0.4 * nz.z);
		float crack = smoothstep(0.06, 0.0, cr) * smoothstep(0.35, 0.6, nz.y);
		emi = vec3(4.0, 1.1, 0.15) * crack * (0.6 + 0.4 * nz.x);
		alb = mix(alb, srgb(vec3(0.9, 0.3, 0.05)), crack);
		bump = 0.9;
	} else if (t == 8) {     // sea bed
		alb = mix(srgb(vec3(0.50, 0.45, 0.33)), srgb(vec3(0.36, 0.34, 0.27)), nz.y);
		bump = 0.2;
	} else if (t == 9) {     // rock
		alb = mix(srgb(vec3(0.24, 0.23, 0.22)), srgb(vec3(0.36, 0.34, 0.31)), smoothstep(0.3, 0.7, nz.y));
		alb *= mix(0.6, 1.0, smoothstep(0.0, 0.1, cr));
		bump = 1.0;
	} else if (t == 10) {    // highlands
		alb = mix(srgb(vec3(0.33, 0.38, 0.20)), srgb(vec3(0.45, 0.42, 0.33)), smoothstep(0.4, 0.7, nz.y));
		bump = 0.8;
	} else {                 // wasteland
		alb = mix(srgb(vec3(0.55, 0.38, 0.24)), srgb(vec3(0.62, 0.47, 0.30)), nz.y);
		alb *= mix(0.6, 1.0, smoothstep(0.0, 0.05, cr));
		bump = 0.6;
	}
	if (t < 12 && t != 8 && ((tex_mask >> t) & 1) != 0) {
		vec3 tx = texture(tex_array, vec3(p * 0.25, float(t))).rgb;
		vec2 q = mat2(vec2(0.8, 0.6), vec2(-0.6, 0.8)) * p * 0.093;
		vec3 tx2 = texture(tex_array, vec3(q, float(t))).rgb;
		tx = mix(tx, tx2, 0.35 + 0.3 * (nz.x - 0.5));
		alb = tx * (0.86 + 0.28 * nz.x);
		if (t == 7) {
			emi = vec3(3.0, 0.8, 0.1) * smoothstep(0.08, 0.3, tx.r - tx.g * 1.4);
		}
	}
}

void fragment() {
	vec2 p = wpos.xz;
	vec4 nz = vec4(texture(noise_tex, p * 0.031).r, texture(noise_tex, p * 0.13 + 0.37).r,
		texture(noise_tex, p * 0.71 + 0.11).r, texture(noise_tex, p * 2.3).r);
	float cr = texture(crack_tex, p * 0.55).r;

	// --- soft blend of the 4 nearest cells around a jittered position
	vec2 pj = p + (vec2(nz.y, texture(noise_tex, p * 0.13 + 0.71).r) - 0.5) * 0.9;
	vec2 q = pj - 0.5;
	ivec2 i0 = ivec2(floor(q));
	vec2 f = smoothstep(0.15, 0.85, fract(q));
	int t00 = cell_type(i0);
	int t10 = cell_type(i0 + ivec2(1, 0));
	int t01 = cell_type(i0 + ivec2(0, 1));
	int t11 = cell_type(i0 + ivec2(1, 1));
	vec3 a00, a10, a01, a11, e00, e10, e01, e11;
	float r00, r10, r01, r11, b00, b10, b01, b11;
	terrain_mat(t00, p, nz, cr, a00, r00, e00, b00);
	if (t10 == t00) { a10 = a00; r10 = r00; e10 = e00; b10 = b00; } else { terrain_mat(t10, p, nz, cr, a10, r10, e10, b10); }
	if (t01 == t00) { a01 = a00; r01 = r00; e01 = e00; b01 = b00; } else { terrain_mat(t01, p, nz, cr, a01, r01, e01, b01); }
	if (t11 == t00) { a11 = a00; r11 = r00; e11 = e00; b11 = b00; } else { terrain_mat(t11, p, nz, cr, a11, r11, e11, b11); }
	vec3 alb = mix(mix(a00, a10, f.x), mix(a01, a11, f.x), f.y);
	float rough = mix(mix(r00, r10, f.x), mix(r01, r11, f.x), f.y);
	vec3 emi = mix(mix(e00, e10, f.x), mix(e01, e11, f.x), f.y);
	float bump = mix(mix(b00, b10, f.x), mix(b01, b11, f.x), f.y);

	// --- cliffs on steep slopes, snow on flat high rock
	float slope = 1.0 - wnrm.y;
	float cliff = smoothstep(0.28, 0.55, slope);
	float strata = texture(noise_tex, vec2((p.x + p.y) * 0.35, wpos.y * 2.5)).r;
	vec3 cliff_col = mix(srgb(vec3(0.20, 0.19, 0.18)), srgb(vec3(0.42, 0.39, 0.35)), smoothstep(0.3, 0.75, strata));
	if (((tex_mask >> 9) & 1) != 0) {
		vec2 side = abs(wnrm.x) > abs(wnrm.z) ? vec2(p.y, wpos.y) : vec2(p.x, wpos.y);
		cliff_col = texture(tex_array, vec3(side * 0.45, 9.0)).rgb * mix(1.0, 1.6, smoothstep(0.3, 0.75, strata));
	}
	cliff_col *= mix(0.7, 1.05, smoothstep(0.02, 0.12, cr));
	if (level == 0) {
		float high = smoothstep(0.35, 0.8, wpos.y) * (1.0 - smoothstep(0.12, 0.3, slope));
		float snowy = high * smoothstep(0.3, 0.5, nz.z * 0.6 + nz.y * 0.6);
		vec3 snow = mix(srgb(vec3(0.78, 0.82, 0.9)), srgb(vec3(0.93, 0.95, 0.98)), nz.y);
		alb = mix(alb, snow, snowy);
		// snow ledges hanging on the cliffs
		cliff_col = mix(cliff_col, snow * 0.9, smoothstep(0.62, 0.7, nz.w) * (1.0 - smoothstep(0.5, 0.75, slope)) * smoothstep(0.1, 0.5, wpos.y));
	} else {
		float top = smoothstep(0.6, 1.0, wpos.y) * (1.0 - cliff);
		alb = mix(alb, alb * 0.35, top);
	}
	alb = mix(alb, cliff_col, cliff);
	rough = mix(rough, 0.85, cliff);
	bump = mix(bump, 1.0, cliff);

	// --- roads and rivers from the path field
	vec2 puv = (p + (vec2(nz.z, nz.w) - 0.5) * 0.05) / map_size;
	vec4 pf = texture(path_tex, puv);
	ivec2 pti = clamp(ivec2(p * 8.0), ivec2(0), ivec2(int(map_size) * 8 - 1));
	vec4 pt = texelFetch(path_tex, pti, 0);
	float road_d = pf.r * 0.7;
	float riv_d = pf.g * 0.7;
	int road_t = (puv.x < 0.0 || puv.y < 0.0 || puv.x > 1.0 || puv.y > 1.0) ? 0 : int(pt.b * 255.0 + 0.5);
	int riv_t = (puv.x < 0.0 || puv.y < 0.0 || puv.x > 1.0 || puv.y > 1.0) ? 0 : int(pt.a * 255.0 + 0.5);
	if (road_t > 0) {
		float w = 0.2 + 0.03 * (nz.y - 0.5);
		float band = 1.0 - smoothstep(w - 0.05, w + 0.03, road_d);
		vec3 rc;
		if (road_t == 1) {
			// dirt track: two ruts with packed ground between
			vec3 packed = (level == 0 && alb.r > 0.5) ? mix(alb, srgb(vec3(0.62, 0.56, 0.5)), 0.45) : srgb(vec3(0.45, 0.35, 0.24));
			float rut = 1.0 - smoothstep(0.018, 0.04, abs(road_d - 0.085));
			rc = mix(packed, srgb(vec3(0.30, 0.22, 0.15)), rut * 0.85);
		} else if (road_t == 2) {
			rc = mix(srgb(vec3(0.42, 0.40, 0.37)), srgb(vec3(0.58, 0.55, 0.5)), smoothstep(0.3, 0.7, texture(noise_tex, p * 1.7).r));
			rc = mix(srgb(vec3(0.33, 0.27, 0.2)), rc, smoothstep(w - 0.02, w - 0.08, road_d));
		} else {
			float stone = smoothstep(0.02, 0.09, texture(crack_tex, p * 0.75).r);
			rc = mix(srgb(vec3(0.22, 0.20, 0.18)), srgb(vec3(0.55, 0.52, 0.47)) * (0.85 + 0.3 * nz.w), stone);
			rc = mix(srgb(vec3(0.33, 0.29, 0.24)), rc, smoothstep(w - 0.01, w - 0.07, road_d));
		}
		alb = mix(alb, rc, band);
		rough = mix(rough, 0.92, band);
		bump = mix(bump, 0.8, band);
	}
	if (riv_t > 0) {
		float w = 0.27 + 0.04 * (nz.y - 0.5);
		float bank = 1.0 - smoothstep(w, w + 0.07, riv_d);
		float water = 1.0 - smoothstep(w - 0.05, w, riv_d);
		alb = mix(alb, alb * 0.72, bank * (1.0 - water));
		vec3 wc;
		float wr = 0.04;
		vec3 we = vec3(0.0);
		float flow = texture(noise_tex, p * 0.9 + vec2(TIME * 0.05, TIME * 0.07)).r;
		float flow2 = texture(noise_tex, p * 2.1 - vec2(TIME * 0.09, TIME * 0.04)).r;
		vec3 clear = mix(srgb(vec3(0.08, 0.24, 0.32)), srgb(vec3(0.17, 0.4, 0.47)), flow);
		clear = mix(clear, srgb(vec3(0.75, 0.82, 0.85)), smoothstep(0.7, 0.8, flow2) * 0.4);
		if (riv_t == 2) {
			// open water in the middle, cracked ice shelves along the banks
			float ice = smoothstep(w * 0.35, w * 0.6, riv_d + (nz.w - 0.5) * 0.08);
			vec3 icec = mix(srgb(vec3(0.6, 0.73, 0.82)), srgb(vec3(0.85, 0.91, 0.95)), smoothstep(0.3, 0.7, nz.z));
			icec *= mix(0.78, 1.0, smoothstep(0.0, 0.05, texture(crack_tex, p * 0.6).r));
			wc = mix(clear, icec, ice);
			wr = mix(0.04, 0.3, ice);
		} else if (riv_t == 3) {
			wc = mix(srgb(vec3(0.2, 0.16, 0.09)), srgb(vec3(0.3, 0.24, 0.14)), flow);
			wr = 0.12;
		} else if (riv_t == 4) {
			wc = srgb(vec3(0.9, 0.35, 0.05));
			we = vec3(1.6, 0.45, 0.05) * (0.6 + 0.5 * flow);
		} else {
			wc = clear;
		}
		alb = mix(alb, wc, water);
		rough = mix(rough, wr, water);
		emi = mix(emi, we, water);
		bump = mix(bump, 0.08, water);
	}

	// --- grid and hover outline
	vec2 fr = fract(p);
	float edge = min(min(fr.x, 1.0 - fr.x), min(fr.y, 1.0 - fr.y));
	if (show_grid) {
		alb *= mix(0.72, 1.0, smoothstep(0.0, 0.025, edge));
	}
	if (floor(p) == hover_cell) {
		float ring = 1.0 - smoothstep(0.03, 0.06, edge);
		alb = mix(alb, srgb(vec3(1.0, 0.85, 0.35)), ring * 0.9);
		emi += vec3(0.6, 0.45, 0.12) * ring;
	}

	// --- fog of war
	float fogv = fog_at(fog_tex, p, map_size, border);
	fogv = smoothstep(0.2, 0.8, fogv + (nz.y - 0.5) * 0.5 + (nz.w - 0.5) * 0.15);
	alb = mix(alb * vec3(0.05, 0.055, 0.07), alb, fogv);
	emi *= fogv;

	// --- detail normal (world XZ perturbation, no tangents needed)
	vec2 dn = texture(noise_nrm, p * 0.45).rg * 2.0 - 1.0;
	dn += (texture(noise_nrm, p * 1.9 + 0.3).rg * 2.0 - 1.0) * 0.5;
	vec3 wn = normalize(wnrm + vec3(dn.x, 0.0, dn.y) * 0.35 * bump);
	NORMAL = normalize((VIEW_MATRIX * vec4(wn, 0.0)).xyz);
	ALBEDO = alb;
	ROUGHNESS = rough;
	SPECULAR = 0.35;
	EMISSION = emi;
}
