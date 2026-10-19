shader_type spatial;
// Vertex-coloured foliage with a gentle wind sway (stronger higher up the model).
render_mode blend_mix, depth_draw_opaque, cull_back, diffuse_burley, specular_schlick_ggx;

uniform float sway = 0.025;
varying float vpos_y;

void vertex() {
	vec3 wp = (MODEL_MATRIX * vec4(0.0, 0.0, 0.0, 1.0)).xyz;
	float ph = wp.x * 0.7 + wp.z * 0.5;
	float k = max(VERTEX.y, 0.0);
	vpos_y = VERTEX.y;
	VERTEX.x += sin(TIME * 1.3 + ph) * sway * k * k;
	VERTEX.z += cos(TIME * 1.1 + ph * 1.3) * sway * 0.6 * k * k;
}

void fragment() {
	ALBEDO = COLOR.rgb;
	ROUGHNESS = 0.85;
	SPECULAR = 0.2;
	// dense needles: little sky light gets in, lower parts darker
	AO = clamp(0.35 + vpos_y * 0.45, 0.35, 0.9);
	AO_LIGHT_AFFECT = 0.25;
	BACKLIGHT = vec3(0.05, 0.07, 0.03);
}
