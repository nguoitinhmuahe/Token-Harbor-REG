import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import gate as g
except ImportError:
    import nine_gate as g


def body(msgs, system=True):
    o = {"model": "a/deepseek-v4.1-flash:free", "max_tokens": 512,
         "messages": msgs}
    if system:
        o["messages"] = [{"role": "system", "content": "sys"}] + msgs
    return json.dumps(o).encode()


def main():
    big = [{"role": "user", "content": "x" * 50000},
           {"role": "assistant", "content": "y" * 50000,
            "tool_calls": [{"id": "1", "type": "function",
                            "function": {"name": "f", "arguments": "{}"}}]},
           {"role": "tool", "tool_call_id": "1",
            "content": [{"type": "text", "text": "z" * 80000}]},
           {"role": "assistant", "content": "ok"},
           {"role": "user", "content": "cau hoi cuoi"}]
    b = body(big)
    vars_ = g._trim_variants(b)
    assert vars_, "ladder phai co it nhat 1 phien ban nho hon"
    sizes = [len(b)] + [len(v) for _, v in vars_]
    assert sizes == sorted(sizes, reverse=True), sizes
    for lbl, v in vars_:
        o = json.loads(v)
        msgs = o["messages"]
        if lbl.startswith("history keep") or lbl == "chi turn cuoi":
            assert not any(m.get("tool_calls") for m in msgs), lbl
            assert not any(m.get("role") == "tool" for m in msgs), lbl
        assert msgs[-1]["role"] == "user", lbl
    assert json.loads(vars_[-1][1])["messages"][-1]["content"] == "cau hoi cuoi"

    mb = g._minimal_body(b)
    o = json.loads(mb)
    assert len(o["messages"]) == 2 and o["messages"][-1]["role"] == "user"

    o = json.loads(g._soft_openai("m", False, g.SOFT_BLOCK))
    assert o["choices"][0]["message"]["content"] == g.SOFT_BLOCK
    o = json.loads(g._soft_anthropic("m", False, g.SOFT_BLOCK))
    assert o["content"][0]["text"] == g.SOFT_BLOCK
    assert g.SOFT_TEXT in json.loads(g._soft_openai("m", False))[
        "choices"][0]["message"]["content"]

    if g.v6_addrs():
        a1 = g.v6_take()
        a2 = g.v6_take()
        assert a1 and a2 and a1 != a2, (a1, a2)

    print("OK: %d phien ban cat, %s -> %s" %
          (len(vars_), len(b), sizes[-1]))


if __name__ == "__main__":
    main()
