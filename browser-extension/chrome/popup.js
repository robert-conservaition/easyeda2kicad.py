// Component type definitions with their relevant parametric fields.
const COMPONENTS = {
  "Capacitor": [
    { id: "capacitance", label: "Capacitance",    placeholder: "e.g. 10uF, 100nF, 1pF" },
    { id: "voltage",     label: "Max Voltage",     placeholder: "e.g. 25V, 50V" },
    { id: "dielectric",  label: "Dielectric/Type", type: "select",
      options: ["", "MLCC (Ceramic)", "Electrolytic", "Tantalum", "Film", "Polymer"] },
    { id: "package",     label: "Package",         placeholder: "e.g. 0402, 0603, 1206" },
  ],
  "Resistor": [
    { id: "resistance",  label: "Resistance",      placeholder: "e.g. 10k, 100R, 4.7M" },
    { id: "power",       label: "Power Rating",     placeholder: "e.g. 0.1W, 1/4W, 1W" },
    { id: "tolerance",   label: "Tolerance",        type: "select",
      options: ["", "0.1%", "0.5%", "1%", "5%", "10%"] },
    { id: "package",     label: "Package",          placeholder: "e.g. 0402, 0603, 0805" },
  ],
  "Inductor": [
    { id: "inductance",  label: "Inductance",       placeholder: "e.g. 10uH, 100nH, 1mH" },
    { id: "current",     label: "Rated Current",    placeholder: "e.g. 1A, 500mA" },
    { id: "resistance",  label: "DCR (max)",        placeholder: "e.g. 100mΩ, 1Ω" },
    { id: "package",     label: "Package",          placeholder: "e.g. 0402, 0603, 1210" },
  ],
  "LED": [
    { id: "color",       label: "Color",            type: "select",
      options: ["", "Red", "Green", "Blue", "White", "Yellow", "Orange", "Amber", "IR", "UV"] },
    { id: "package",     label: "Package",          placeholder: "e.g. 0402, 0603, 3mm, 5mm" },
    { id: "voltage",     label: "Forward Voltage",  placeholder: "e.g. 2.0V, 3.2V" },
    { id: "current",     label: "Forward Current",  placeholder: "e.g. 20mA" },
  ],
  "Transistor": [
    { id: "polarity",    label: "Type",             type: "select",
      options: ["", "NPN BJT", "PNP BJT", "N-channel MOSFET", "P-channel MOSFET", "IGBT"] },
    { id: "voltage",     label: "Vce / Vds (max)",  placeholder: "e.g. 30V, 100V" },
    { id: "current",     label: "Ic / Id (max)",    placeholder: "e.g. 1A, 100mA" },
    { id: "package",     label: "Package",          placeholder: "e.g. SOT-23, TO-220" },
  ],
  "Diode": [
    { id: "diode_type",  label: "Type",             type: "select",
      options: ["", "Rectifier", "Schottky", "Zener", "TVS", "Signal", "Fast Recovery"] },
    { id: "voltage",     label: "Reverse Voltage",  placeholder: "e.g. 40V, 100V" },
    { id: "current",     label: "Forward Current",  placeholder: "e.g. 1A, 200mA" },
    { id: "package",     label: "Package",          placeholder: "e.g. SOD-123, SMA, DO-214" },
  ],
  "Voltage Regulator": [
    { id: "reg_type",    label: "Topology",         type: "select",
      options: ["", "LDO Linear", "Buck", "Boost", "Buck-Boost", "Charge Pump"] },
    { id: "voltage",     label: "Output Voltage",   placeholder: "e.g. 3.3V, 5V, Adj" },
    { id: "current",     label: "Output Current",   placeholder: "e.g. 500mA, 1A, 3A" },
    { id: "package",     label: "Package",          placeholder: "e.g. SOT-23, TO-252, DFN" },
  ],
  "Crystal / Oscillator": [
    { id: "frequency",   label: "Frequency",        placeholder: "e.g. 16MHz, 32.768kHz" },
    { id: "xtal_type",   label: "Type",             type: "select",
      options: ["", "Crystal (passive)", "Oscillator (active)", "TCXO", "VCXO", "MEMS"] },
    { id: "load_cap",    label: "Load Capacitance", placeholder: "e.g. 12pF, 18pF" },
    { id: "package",     label: "Package",          placeholder: "e.g. 3225, 5032, HC-49" },
  ],
  "Op-Amp": [
    { id: "channels",    label: "Channels",         type: "select",
      options: ["", "Single", "Dual", "Quad"] },
    { id: "bandwidth",   label: "GBW (min)",        placeholder: "e.g. 1MHz, 10MHz" },
    { id: "supply",      label: "Supply Voltage",   placeholder: "e.g. 5V, ±15V, 3.3V" },
    { id: "package",     label: "Package",          placeholder: "e.g. SOT-23-5, SOIC-8" },
  ],
  "MCU / SoC": [
    { id: "family",      label: "Family / Brand",   placeholder: "e.g. STM32, ESP32, ATmega, nRF52" },
    { id: "flash",       label: "Flash",            placeholder: "e.g. 256kB, 1MB" },
    { id: "ram",         label: "RAM",              placeholder: "e.g. 64kB, 512kB" },
    { id: "package",     label: "Package",          placeholder: "e.g. LQFP-64, QFN-32" },
  ],
};

// ── DOM setup ─────────────────────────────────────────────────────────────────

const typeSelect  = document.getElementById("type");
const paramsDiv   = document.getElementById("params");
const btnLcsc     = document.getElementById("btn-lcsc");
const btnJlcpcb   = document.getElementById("btn-jlcpcb");

// Populate component type dropdown
Object.keys(COMPONENTS).forEach((name) => {
  const opt = document.createElement("option");
  opt.value = opt.textContent = name;
  typeSelect.appendChild(opt);
});

function renderParams(typeName) {
  paramsDiv.innerHTML = "";
  (COMPONENTS[typeName] || []).forEach((field) => {
    const wrap = document.createElement("div");
    wrap.className = "field";

    const label = document.createElement("label");
    label.htmlFor = field.id;
    label.textContent = field.label;
    wrap.appendChild(label);

    let input;
    if (field.type === "select") {
      input = document.createElement("select");
      field.options.forEach((opt) => {
        const o = document.createElement("option");
        o.value = opt;
        o.textContent = opt || "Any";
        input.appendChild(o);
      });
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.placeholder = field.placeholder || "";
    }
    input.id = field.id;
    wrap.appendChild(input);
    paramsDiv.appendChild(wrap);
  });
}

typeSelect.addEventListener("change", () => renderParams(typeSelect.value));
renderParams(typeSelect.value); // initial render

// ── query builder ─────────────────────────────────────────────────────────────

function buildQuery() {
  const terms = [];
  paramsDiv.querySelectorAll("input, select").forEach((el) => {
    const v = el.value.trim();
    if (v && v !== "Any") terms.push(v);
  });
  terms.push(typeSelect.value.toLowerCase().split(" / ")[0]);
  return terms.join(" ");
}

btnLcsc.addEventListener("click", () => {
  const q = buildQuery();
  chrome.tabs.create({ url: `https://www.lcsc.com/search?q=${encodeURIComponent(q)}` });
});

btnJlcpcb.addEventListener("click", () => {
  const q = buildQuery();
  chrome.tabs.create({ url: `https://jlcpcb.com/parts?searchTxt=${encodeURIComponent(q)}` });
});
