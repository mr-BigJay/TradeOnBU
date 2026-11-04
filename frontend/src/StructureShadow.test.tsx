import {render,screen,fireEvent} from "@testing-library/react";
import {vi,it,expect} from "vitest";
import {StructureShadow} from "./StructureShadow";
import {apiFetch} from "./api";
vi.mock("./api",()=>({apiFetch:vi.fn()}));
it("shows source candles and late touch separately",async()=>{
 vi.mocked(apiFetch).mockResolvedValue({enabled:true,counts:[],items:[{state:"late_touch",observed_ms:900000,
 evidence:{key:"x",variant:"swing",direction:"LONG",reference:99,pivot:104,zone:[102,103],sweep_ms:0,break_ms:300000,fvg_ms:600000,token:"MSS",
 bars:[[0,100,102,98,101],[300000,101,106,100,105],[600000,105,107,103,106]]},
 tracking:{entry:102.5,stop:97.9}}]});
 render(<StructureShadow/>);
 fireEvent.click(await screen.findByRole("button",{name:/برخورد پیش از شناسایی/}));
 expect(screen.getByRole("img",{name:"کندل‌های مرجع ستاپ"})).toBeInTheDocument();
 expect(screen.getByText(/بررسی نشده/)).toBeInTheDocument();
});
