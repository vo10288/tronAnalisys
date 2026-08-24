from tronanalysis.tron import parse_native, parse_trc20


def test_parse_trc20():
    record = {
        "type": "Transfer",
        "from": "TA111111111111111111111111111111111",
        "to": "TB222222222222222222222222222222222",
        "value": "1234500",
        "block_timestamp": 1700000000000,
        "transaction_id": "abc",
        "token_info": {"symbol": "USDT", "decimals": "6", "address": "TC333333333333333333333333333333333"},
    }
    t = parse_trc20(record)
    assert t is not None
    assert t.amount == 1.2345
    assert t.asset == "USDT"
    assert t.txid == "abc"


def test_parse_native_transfer_contract():
    tx = {
        "txID": "tx1",
        "block_timestamp": 1700000000000,
        "raw_data": {"contract": [{"type": "TransferContract", "parameter": {"value": {
            "owner_address": "41" + "00" * 20,
            "to_address": "41" + "11" * 20,
            "amount": 2_000_000,
        }}}]},
    }
    t = parse_native(tx)
    assert t is not None
    assert t.amount == 2.0
    assert t.src.startswith("T") and t.dst.startswith("T")
