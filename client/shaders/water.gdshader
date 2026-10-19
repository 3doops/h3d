shader_type spatial;
render_mode blend_mix, depth_draw_always, cull_back, diffuse_burley, specular_schlick_ggx;

uniform sampler2D noise_nrm : filter_linear_mipmap, repeat_enable;
uniform sampler2D noise_tex : filter_linear_mipmap, repeat_enable;
uniform sampler2D fog_tex : filter_linear, repeat_disable;
uniform sampler2D depth_tex : hint_depth_texture, filter_linear;
uniform sampler2D screen_tex : hint_screen_texture, filter_linear_mipmap;
uniform float map_size = 48.0;
uniform float border = 10.0;
uniform bool underground = false;

varying vec3 wpos;

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
	wpos = (MODEL_MATRIX * vec4(VERTEX, 1.0)).xyz;
}

void fragment() {
	vec2 p = wpos.xz;
	float t = TIME;
	vec2 n1 = texture(noise_nrm, p * 0.09 + vec2(t * 0.018, t * 0.011)).rg * 2.0 - 1.0;
	vec2 n2 = texture(noise_nrm, p * 0.21 - vec2(t * 0.025, -t * 0.02)).rg * 2.0 - 1.0;
	vec2 n3 = texture(noise_nrm, p * 0.6 + vec2(-t * 0.05, t * 0.04)).rg * 2.0 - 1.0;
	vec2 dn = n1 * 0.6 + n2 * 0.4 + n3 * 0.25;
	vec3 wn = normalize(vec3(dn.x * 0.35, 1.0, dn.y * 0.35));
	NORMAL = normalize((VIEW_MATRIX * vec4(wn, 0.0)).xyz);

	// water thickness along the view ray from the depth buffer (reverse-Z, Godot 4.3+)
	float depth = texture(depth_tex, SCREEN_UV).r;
	vec4 vpos = INV_PROJECTION_MATRIX * vec4(SCREEN_UV * 2.0 - 1.0, depth, 1.0);
	vpos.xyz /= vpos.w;
	float thick = max(-vpos.z - (-VERTEX.z), 0.0);
	// convert to approximate vertical depth
	vec3 vdir = normalize(VERTEX);
	float vert = thick * abs(dot(vdir, (VIEW_MATRIX * vec4(0.0, 1.0, 0.0, 0.0)).xyz));

	vec3 shallow = underground ? srgb(vec3(0.05, 0.16, 0.18)) : srgb(vec3(0.16, 0.48, 0.52));
	vec3 deep = underground ? srgb(vec3(0.01, 0.03, 0.05)) : srgb(vec3(0.03, 0.15, 0.34));
	float dfac = smoothstep(0.0, 0.55, vert);
	vec3 col = mix(shallow, deep, dfac);
	vec3 refr = texture(screen_tex, SCREEN_UV + dn * 0.012 * (1.0 - dfac)).rgb;
	col = mix(refr * shallow * 2.2, col, smoothstep(0.0, 0.18, vert));

	// surf: foam right at the shore plus bands rolling in
	float nfoam = texture(noise_tex, p * 0.8 + vec2(t * 0.03, -t * 0.02)).r;
	float shore = 1.0 - smoothstep(0.0, 0.07 + 0.05 * nfoam, vert);
	float bands = smoothstep(0.82, 0.97, fract(vert * 3.2 - t * 0.22 + nfoam * 0.6)) * (1.0 - smoothstep(0.05, 0.42, vert));
	float foam = clamp(shore + bands * smoothstep(0.45, 0.6, nfoam + 0.15), 0.0, 1.0);
	// crests in open water
	// wave tint: lighter crests, darker troughs
	float wv = texture(noise_tex, p * 0.18 + vec2(t * 0.012, t * 0.008)).r;
	col *= 0.85 + 0.35 * wv;
	if (underground) foam *= 0.3;
	col = mix(col, vec3(0.85, 0.9, 0.93), foam);

	float fogv = fog_at(fog_tex, p, map_size, border);
	fogv = smoothstep(0.2, 0.8, fogv + (texture(noise_tex, p * 0.13 + 0.37).r - 0.5) * 0.5);
	col = mix(col * vec3(0.05, 0.055, 0.07), col, fogv);

	ALBEDO = col;
	ROUGHNESS = mix(0.04, 0.6, foam);
	SPECULAR = mix(0.5, 0.2, foam) * fogv;
	METALLIC = 0.0;
}
