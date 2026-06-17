"""Seed an editable Bangla solar-panel IVR demo.

Run:
    .venv\\Scripts\\python.exe seed_demo.py

The demo intentionally leaves transfer numbers blank so no private number is
published. Edit the transfer node in the UI before testing live transfer.
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import audio
import db


def add_node(flow_id: int, name: str, ntype: str, text: str,
             *, transfer: str = "", timeout: int = 8, retries: int = 2) -> int:
    print(f"Generating audio: {name}")
    prompt_audio = audio.create_gtts_audio(text, "bn") if text else ""
    return db.insert_returning_id(
        """
        INSERT INTO flow_nodes
            (flow_id, name, node_type, prompt_text, prompt_audio, prompt_lang,
             transfer_number, gather_timeout_seconds, max_retries)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
        """,
        (flow_id, name, ntype, text, prompt_audio, "bn", transfer, timeout, retries),
    )


def link(node_id: int, digit: str, next_id: int, label: str) -> None:
    db.execute(
        """
        INSERT INTO flow_transitions (node_id, digit, next_node_id, label)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (node_id, digit) DO UPDATE
            SET next_node_id = excluded.next_node_id, label = excluded.label
        """,
        (node_id, digit, next_id, label),
    )


def build() -> int:
    flow_id = db.insert_returning_id(
        "INSERT INTO flows (name, description) VALUES (?, ?) RETURNING id",
        (
            "ডেমো সৌর প্যানেল ব্যবসা IVR",
            "বাংলা সোলার প্যানেল ব্যবসার নমুনা IVR: কোটেশন, প্যাকেজ, সার্ভিস, EMI এবং প্রতিনিধি।",
        ),
    )

    main = add_node(
        flow_id, "মূল মেনু", "menu",
        "সোলার স্মার্ট বাংলাদেশে আপনাকে স্বাগতম। নতুন সোলার প্যানেল কোটেশনের জন্য ১ চাপুন। "
        "বাসা ও ব্যবসার প্যাকেজ জানতে ২ চাপুন। ইনস্টলেশন বা সার্ভিস সাপোর্টের জন্য ৩ চাপুন। "
        "ই এম আই এবং পেমেন্ট তথ্য জানতে ৪ চাপুন। প্রতিনিধির সাথে কথা বলতে ০ চাপুন।",
    )
    quote = add_node(
        flow_id, "নতুন কোটেশন", "menu",
        "নতুন কোটেশনের জন্য ধন্যবাদ। বাসার সোলার সিস্টেমের জন্য ১ চাপুন। দোকান বা অফিসের জন্য ২ চাপুন। "
        "সেচ পাম্প বা কৃষি ব্যবহারের জন্য ৩ চাপুন। মূল মেনুতে ফিরতে ৯ চাপুন।",
    )
    packages = add_node(
        flow_id, "প্যাকেজ তথ্য", "menu",
        "আমাদের জনপ্রিয় প্যাকেজগুলো শুনুন। এক কিলোওয়াট হোম প্যাকেজ জানতে ১ চাপুন। "
        "তিন কিলোওয়াট ব্যবসা প্যাকেজ জানতে ২ চাপুন। ব্যাটারি ব্যাকআপ প্যাকেজ জানতে ৩ চাপুন। "
        "মূল মেনুতে ফিরতে ৯ চাপুন।",
    )
    support = add_node(
        flow_id, "সার্ভিস সাপোর্ট", "menu",
        "সার্ভিস সাপোর্ট বিভাগে স্বাগতম। ইনভার্টার সমস্যার জন্য ১ চাপুন। ব্যাটারি সমস্যার জন্য ২ চাপুন। "
        "প্যানেল পরিষ্কার বা মেইনটেন্যান্সের জন্য ৩ চাপুন। প্রতিনিধির সাথে কথা বলতে ০ চাপুন। মূল মেনুতে ফিরতে ৯ চাপুন।",
    )
    emi = add_node(
        flow_id, "EMI ও পেমেন্ট", "message",
        "আমাদের সোলার প্যাকেজে সহজ মাসিক কিস্তি সুবিধা আছে। সাধারণত ত্রিশ শতাংশ ডাউন পেমেন্ট দিয়ে কিস্তি শুরু করা যায়। "
        "বিস্তারিত জানতে আমাদের প্রতিনিধি আপনার সাথে যোগাযোগ করবে। ধন্যবাদ।",
    )
    agent = add_node(
        flow_id, "প্রতিনিধির কাছে ট্রান্সফার", "transfer",
        "আপনাকে একজন প্রতিনিধির সাথে সংযুক্ত করা হচ্ছে। অনুগ্রহ করে লাইনে থাকুন।",
        transfer="",
    )
    home_quote = add_node(flow_id, "বাসার কোটেশন", "message", "বাসার জন্য আমরা আপনার মাসিক বিদ্যুৎ বিল, ছাদের জায়গা এবং ব্যাকআপ প্রয়োজন দেখে কোটেশন দিই।")
    business_quote = add_node(flow_id, "ব্যবসার কোটেশন", "message", "দোকান, অফিস বা কারখানার জন্য লোড ক্যালকুলেশন করে অন-গ্রিড অথবা হাইব্রিড সোলার সিস্টেম দেওয়া হয়।")
    pump_quote = add_node(flow_id, "সেচ পাম্প কোটেশন", "message", "সেচ পাম্পের জন্য পাম্পের হর্স পাওয়ার, দৈনিক চালানোর সময় এবং জমির দূরত্ব অনুযায়ী সিস্টেম ডিজাইন করা হয়।")
    home_pkg = add_node(flow_id, "১ কিলোওয়াট হোম প্যাকেজ", "message", "এক কিলোওয়াট হোম প্যাকেজে প্যানেল, ইনভার্টার, স্ট্রাকচার এবং ইনস্টলেশন অন্তর্ভুক্ত থাকে।")
    business_pkg = add_node(flow_id, "৩ কিলোওয়াট ব্যবসা প্যাকেজ", "message", "তিন কিলোওয়াট ব্যবসা প্যাকেজ দোকান, অফিস এবং ছোট প্রতিষ্ঠানের জন্য উপযোগী।")
    backup_pkg = add_node(flow_id, "ব্যাটারি ব্যাকআপ প্যাকেজ", "message", "ব্যাটারি ব্যাকআপ প্যাকেজে হাইব্রিড ইনভার্টার এবং ব্যাটারি অপশন আছে।")
    inverter_support = add_node(flow_id, "ইনভার্টার সাপোর্ট", "message", "ইনভার্টারে অ্যালার্ম বা এরর দেখালে মেইন সুইচ বন্ধ করে পাঁচ মিনিট পরে চালু করুন।")
    battery_support = add_node(flow_id, "ব্যাটারি সাপোর্ট", "message", "ব্যাটারির ব্যাকআপ কমে গেলে কানেকশন, চার্জিং এবং লোড পরীক্ষা করা প্রয়োজন।")
    cleaning_support = add_node(flow_id, "প্যানেল মেইনটেন্যান্স", "message", "সোলার প্যানেল নিয়মিত পরিষ্কার রাখলে উৎপাদন ভালো থাকে।")

    link(main, "1", quote, "নতুন কোটেশন")
    link(main, "2", packages, "প্যাকেজ")
    link(main, "3", support, "সাপোর্ট")
    link(main, "4", emi, "EMI")
    link(main, "0", agent, "প্রতিনিধি")
    link(quote, "1", home_quote, "বাসা")
    link(quote, "2", business_quote, "ব্যবসা")
    link(quote, "3", pump_quote, "সেচ পাম্প")
    link(quote, "9", main, "মূল মেনু")
    link(packages, "1", home_pkg, "১ কিলোওয়াট")
    link(packages, "2", business_pkg, "৩ কিলোওয়াট")
    link(packages, "3", backup_pkg, "ব্যাকআপ")
    link(packages, "9", main, "মূল মেনু")
    link(support, "1", inverter_support, "ইনভার্টার")
    link(support, "2", battery_support, "ব্যাটারি")
    link(support, "3", cleaning_support, "মেইনটেন্যান্স")
    link(support, "0", agent, "প্রতিনিধি")
    link(support, "9", main, "মূল মেনু")
    db.execute("UPDATE flows SET root_node_id = ? WHERE id = ?", (main, flow_id))
    return flow_id


if __name__ == "__main__":
    db.init_db()
    fid = build()
    print(f"Demo flow created. Flow ID: {fid}")
